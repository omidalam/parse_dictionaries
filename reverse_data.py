"""
Parse Apple dictionaries given as Body.data files.

The function that does the heavy lifting is _parse. Overview:

- The files are just ZIPs of XML entries concatenated with some headers
  inbetween
- We greedily try to find the ZIPs and extract the XML
- Some XML parsing is implemented to find interesting stuff (derivatives for
  example).

"""
import argparse
import collections
import contextlib
import itertools
import os
import pickle
import shutil
import zlib
from typing import Dict, List, Tuple, Set

import lxml.etree as etree

# New Oxford American Dictionary
NOAD = '/System/Library/AssetsV2/' \
       'com_apple_MobileAsset_DictionaryServices_dictionaryOSX/' \
       '4094df88727a054b658681dfb74f23702d3c985e.asset/' \
       'AssetData/' \
       'New Oxford American Dictionary.dictionary/' \
       'Contents/Resources/Body.data'
french =  '/System/Library/AssetsV2/'\
          'com_apple_MobileAsset_DictionaryServices_dictionaryOSX/'\
          'c214f3e2ba8f0b26ce1d381fa76a92e09e927b7a.asset/'\
          'AssetData/French - English.dictionary/'\
          'Contents/Resources/Body.data'


# Matches spans that give some meta info, like "literary", "informal", etc.
XPATH_INFO = '//span[@class="lg"]/span[@class="reg"]'

# This matches the bold words in the definitions. For an example,
# see "vital", which contains "noun (vitals)"
XPATH_OTHER_WORDS = '//span[@class="fg"]/span[@class="f"]'

# This matches the derivatives at the end of definition.
XPATH_DERIVATIVES = '//span[contains(@class, "t_derivatives")]//' \
                    'span[contains(@class, "x_xoh")]/' \
                    'span[@role="text"]'

OUTPUT_HTML_HEADER = """
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Words</title>
  <link rel="stylesheet" href="DefaultStyle.css">
  <link rel="stylesheet" href="CustomStyle.css">
</head>
"""

CUSTOM_CSS = """
.div-entry {
    border-top: 2px solid black;
    padding-bottom: 50px;
}
"""


def main():
  p = argparse.ArgumentParser()
  p.add_argument('--dictionary_path', default=french,
                 help=f"path to a body.data file. defaults to {french}")
  p.add_argument('--lookup', nargs='+',
                 default=['vital', 'house', 'cozen'],
                 help='words to lookup')
  p.add_argument('--output_path', default='lookup/lookup.html',
                 help='where to save the words.')

  flags = p.parse_args()
  save_definitions(flags.dictionary_path,
                   flags.lookup,
                   flags.output_path)


def save_definitions(dictionary_path, lookup_words, output_path):
  if not dictionary_path.endswith('Body.data'):
    raise ValueError(f'Expected a Body.data file, got {dictionary_path}')

  word_dict = WordDictionary.from_file(dictionary_path)
  os.makedirs(os.path.dirname(output_path), exist_ok=True)

  with open(output_path, 'w') as f:
    f.write(OUTPUT_HTML_HEADER)
    with wrap_in_tag(f, 'body'):
      for target in lookup_words:
        entry = word_dict[target]
        t = entry.get_xml_tree()
        with wrap_in_tag(f, 'div', attr='class="div-entry"'):
          f.write(etree.tostring(t, pretty_print=True).decode())

  print(f'Saved {len(lookup_words)} definitions at {output_path}.')

  # Copy default and custom CSS to output
  css_path = dictionary_path.replace('Body.data', 'DefaultStyle.css')
  if not os.path.isfile(css_path):
    print(f'WARN: CSS not found at expected path {css_path}')
  css_path_out = os.path.join(os.path.dirname(output_path),
                              os.path.basename(css_path))
  shutil.copy(css_path, css_path_out)
  custom_css_path_out = os.path.join(os.path.dirname(output_path),
                                     'CustomStyle.css')
  with open(custom_css_path_out, 'w') as f:
    f.write(CUSTOM_CSS)


class WordDictionary:
  """Rrepresents a dictionary."""

  @staticmethod
  def from_file(p):
    d, links = parse(p)
    return WordDictionary(d, links)

  def __init__(self, d: Dict[str, 'Entry'], links: Dict[str, str]):
    """Constructor.

    :param d: The dictionary, as a dict mapping words to Entry instances.
    :param links: Special links, as a dict mapping words to words. Words `w` in
      this dict have a definition at `links[w]`.
    """
    self.d, self.links = d, links

  def items(self):
    return self.d.items()

  def add_links(self, links: Dict[str, str]):
    for w, linked_w in links.items():
      # Word already linked, so we should be able to find it.
      if w in self.links:
        continue
      assert linked_w in self
      self.links[w] = linked_w

  def filtered(self, words) -> 'WordDictionary':
    filtered_dict = {}
    filtered_links = {}
    for w in words:
      filtered_dict[w] = self[w]  # May raise!
      if w in self.links:
        filtered_links[w] = self.links[w]
    return WordDictionary(filtered_dict, filtered_links)

  def __getitem__(self, w) -> 'Entry':
    if w in self.d:
      return self.d[w]
    if w in self.links:
      return self.d[self.links[w]]
    raise KeyError(w)

  def __contains__(self, w):
    return w in self.d or w in self.links

  def __str__(self):
    return f'WordDcitionary({len(self.d)} definitions, ' \
           f'{len(self.links)} links)'


@contextlib.contextmanager
def wrap_in_tag(f, tag, attr=None):
  if attr:
    f.write(f'<{tag} {attr}>')
  else:
    f.write(f'<{tag}>')
  yield
  f.write(f'</{tag}>')


def parse(dictionary_path):
  print(f"Parsing {dictionary_path}...")
  entries_tuples = _parse(dictionary_path)
  print('Augmenting...')
  # Some definitions have multiple entries (for example foil in NOAD).
  # Merge them here.
  entries = merge_same_keys(entries_tuples)
  links = _get_links(dictionary_path, entries)
  print(f'Links: {len(links)}')
  return entries, links


def merge_same_keys(entries_tuples: List[Tuple[str, str]]) -> Dict[str, 'Entry']:
  entries = {}
  for k, e in entries_tuples:
    if k in entries:
      entries[k].append_definition(e)
    else:
      entries[k] = Entry(k, e)
  return entries


def _pickle_cache(p):
  """Little helper decorator to store stuff in a pickle cache, used below."""
  def decorator(func):
    if os.path.isfile(p):
      with open(p, 'rb') as f:
        cache = pickle.load(f)
    else:
      cache = {}

    def new_func(*args, **kwargs):
      key = args[0]
      if key not in cache:
        res = func(*args, **kwargs)
        cache[key] = res
        with open(p, 'wb') as f:
          pickle.dump(cache, f)
      else:
        print(f'Cached in {p}: {key}')
      return cache[key]

    return new_func
  return decorator


@_pickle_cache('cache_links.pkl')
def _get_links(p, entries):
  del p  # Only used for cache
  links = {}
  print('Getting links...')
  for i, (key, entry) in enumerate(entries.items()):
    if i % 1000 == 0:
      progress = i / len(entries)
      print(f'\rGetting links: {progress * 100:.1f}%', end='', flush=True)
    for w in entry.get_words_and_derivaties():
      if w in entries:
        continue
      # Word is not in dictionary, add to links
      if w in links:
        continue
      links[w] = key
  return links


@_pickle_cache('cache_parse.pkl')
def _parse(dictionary_path) -> List[Tuple[str, str]]:
  """Parse Body.data into a list of entries given as key, definition tuples."""
  with open(dictionary_path, 'rb') as f:
    content_bytes = f.read()
  total_bytes = len(content_bytes)

  # The first zip file starts at ~100 bytes:
  content_bytes = content_bytes[100:]

  first = True
  entries = []
  for i in itertools.count():
    if not content_bytes:  # Backup condition in case stop is never True.
      break
    try:
      d = zlib.decompressobj()
      res = d.decompress(content_bytes)
      new_entries, stop = _split(res, verbose=first)
      entries += new_entries
      if stop:
        break
      if i % 10 == 0:
        bytes_left = len(content_bytes)  # Approximately...
        progress = 1 - bytes_left / total_bytes
        print(f'{progress * 100:.1f}% // '
              f'{len(entries)} entries parsed // '
              f'Latest entry: {entries[-1][0]}')
      first = False

      # Set content_bytes to the unused data so we can start the search for the
      # next zip file.
      content_bytes = d.unused_data

    except zlib.error:  # Current content_bytes is not a zipfile -> skip a byte.
      content_bytes = content_bytes[1:]

  return entries


def _split(input_bytes, verbose) -> Tuple[List[Tuple[str, str]],
                                          bool]:
  """Split `input_bytes` into a list of tuples (name, definition)."""
  printv = print if verbose else lambda *a, **k: ...

  # The first four bytes are always not UTF-8 (not sure why?)
  input_bytes = input_bytes[4:]

  printv('Splitting...')
  printv(f'{"index": <10}', f'{"bytes": <30}', f'{"as chars"}',
         '-' * 50, sep='\n')

  entries = []
  total_offset = 0
  stop_further_parsing = False

  while True:
    # Find the next newline, which delimits the current entry.
    try:
      next_offset = input_bytes.index('\n'.encode('utf-8'))
    except ValueError:  # No more new-lines -> no more entries!
      break

    entry_text = input_bytes[:next_offset].decode('utf-8')

    # The final part of the dictionary contains some meta info, which we skip.
    # TODO: might only be for the NOAD, so check other dictionaries.
    if 'fbm_AdvisoryBoard' in entry_text[:1000]:
      print('fbm_AdvisoryBoard detected, stopping...')
      stop_further_parsing = True
      break

    # Make sure we have a valid entry.
    assert (entry_text.startswith('<d:entry') and
            entry_text.endswith('</d:entry>')), \
      f'ENTRY: {entry_text} \n REM: {input_bytes}'

    # The name of the definition is stored in the "d:title" attribute,
    # where "d" is the current domain, which we get from the nsmap - the
    # actual attribute will be "{com.apple.blabla}title" (including the
    # curly brackets).
    xml_entry = etree.fromstring(entry_text)
    domain = xml_entry.nsmap['d']
    key = '{%s}title' % domain
    name = xml_entry.get(key)  # Lookup the attribute in the tree.

    entries.append((name, entry_text))

    printv(f'{next_offset + total_offset: 10d}',
           f'{str(input_bytes[next_offset + 1:next_offset + 5]): <30}',
           xml_entry.get(key))

    # There is always 4 bytes of chibberish between entries. Skip them
    # and the new lines (for a total of 5 bytes).
    input_bytes = input_bytes[next_offset + 5:]
    total_offset += next_offset
  return entries, stop_further_parsing


class Entry:
  def __init__(self, key, content):
    self.key = key
    self.content = content

    # Set to true on the first call to `append_definition`.
    # Used in get_xml_tree.
    self._multi_definition = False

    # These are lazily populated as they take a while.
    self._xml = None
    self._info = None
    self._words_and_derivatives = None

  def append_definition(self, content):
    """Extend self.content with more XML.

    The key here is to make sure the overall content is still valid XML
    by wrapping the whole thing in a <div>, which is handled in `get_xml_tree`,
    here we just set _multi_definition.
    """
    self._multi_definition = True
    self.content += content

  def get_xml_tree(self):
    content = self.content
    if self._multi_definition:
      content = '<div>' + self.content + '</div>'
    return etree.fromstring(content)

  def get_special(self, xpath, replace=None):
    matches = self.get_xml().xpath(xpath)
    if not matches:
      return []
    # Note: May be empty.
    texts = [el.text for el in matches if el.text]
    if replace:
      for r_in, r_out in replace:
        texts = [t.replace(r_in, r_out) for t in texts]
    texts = [t.strip() for t in texts]
    return texts

  def get_xml(self):
    if self._xml is None:
      self._xml = self.get_xml_tree()
    return self._xml

  def get_words_and_derivaties(self):
    def _make():
      derivatives = set(self.get_special(XPATH_DERIVATIVES))
      other_words = set(self.get_special(XPATH_OTHER_WORDS, [("the", "")]))
      return (derivatives | other_words) - {self.key}

    return _lazy(self, "_words_and_derivatives", _make)

  def get_info(self):
    return _lazy(self, "_info", lambda: set(self.get_special(XPATH_INFO)))

  def __str__(self):
    return f'Entry({self.key})'


def _lazy(obj, ivar, creator):
  if getattr(obj, ivar) is None:
    setattr(obj, ivar, creator())
  return getattr(obj, ivar)


if __name__ == '__main__':
  main()
