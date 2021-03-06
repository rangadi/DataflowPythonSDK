# Copyright 2016 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for all code snippets used in public docs."""

import logging
import sys
import tempfile
import unittest

import google.cloud.dataflow as df
from google.cloud.dataflow import pvalue
from google.cloud.dataflow import typehints
from google.cloud.dataflow.examples.snippets import snippets
from google.cloud.dataflow.utils.options import PipelineOptions
from google.cloud.dataflow.utils.options import TypeOptions


class ParDoTest(unittest.TestCase):
  """Tests for dataflow/model/par-do."""

  def test_pardo(self):
    # Note: "words" and "ComputeWordLengthFn" are referenced by name in
    # the text of the doc.

    words = ['aa', 'bbb', 'c']
    # [START model_pardo_pardo]
    class ComputeWordLengthFn(df.DoFn):
      def process(self, context):
        return [len(context.element)]
    # [END model_pardo_pardo]

    # [START model_pardo_apply]
    # Apply a ParDo to the PCollection "words" to compute lengths for each word.
    word_lengths = words | df.ParDo(ComputeWordLengthFn())
    # [END model_pardo_apply]
    self.assertEqual({2, 3, 1}, set(word_lengths))

  def test_pardo_yield(self):
    words = ['aa', 'bbb', 'c']
    # [START model_pardo_yield]
    class ComputeWordLengthFn(df.DoFn):
      def process(self, context):
        yield len(context.element)
    # [END model_pardo_yield]

    word_lengths = words | df.ParDo(ComputeWordLengthFn())
    self.assertEqual({2, 3, 1}, set(word_lengths))

  def test_pardo_using_map(self):
    words = ['aa', 'bbb', 'c']
    # [START model_pardo_using_map]
    word_lengths = words | df.Map(len)
    # [END model_pardo_using_map]

    self.assertEqual({2, 3, 1}, set(word_lengths))

  def test_pardo_using_flatmap(self):
    words = ['aa', 'bbb', 'c']
    # [START model_pardo_using_flatmap]
    word_lengths = words | df.FlatMap(lambda word: [len(word)])
    # [END model_pardo_using_flatmap]

    self.assertEqual({2, 3, 1}, set(word_lengths))

  def test_pardo_using_flatmap_yield(self):
    words = ['aA', 'bbb', 'C']
    # [START model_pardo_using_flatmap_yield]
    def capitals(word):
      for letter in word:
        if 'A' <= letter <= 'Z':
            yield letter
    all_capitals = words | df.FlatMap(capitals)
    # [END model_pardo_using_flatmap_yield]

    self.assertEqual({'A', 'C'}, set(all_capitals))

  def test_pardo_with_label(self):
    words = ['aa', 'bbc', 'defg']
    # [START model_pardo_with_label]
    result = words | df.Map('CountUniqueLetters', lambda word: len(set(word)))
    # [END model_pardo_with_label]

    self.assertEqual({1, 2, 4}, set(result))

  def test_pardo_side_input(self):
    p = df.Pipeline('DirectPipelineRunner')
    words = p | df.Create('start', ['a', 'bb', 'ccc', 'dddd'])

    # [START model_pardo_side_input]
    # Callable takes additional arguments.
    def filter_using_length(word, lower_bound, upper_bound=float('inf')):
      if lower_bound <= len(word) <= upper_bound:
        yield word

    # Construct a deferred side input.
    avg_word_len = words | df.Map(len) | df.CombineGlobally(df.combiners.MeanCombineFn())

    # Call with explicit side inputs.
    small_words = words | df.FlatMap('small', filter_using_length, 0, 3)

    # A single deferred side input.
    larger_than_average = words | df.FlatMap('large',
                                             filter_using_length,
                                             lower_bound=pvalue.AsSingleton(avg_word_len))

    # Mix and match.
    small_but_nontrivial = words | df.FlatMap(filter_using_length,
                                              lower_bound=2,
                                              upper_bound=pvalue.AsSingleton(avg_word_len))
    # [END model_pardo_side_input]

    df.assert_that(small_words, df.equal_to(['a', 'bb', 'ccc']))
    df.assert_that(larger_than_average, df.equal_to(['ccc', 'dddd']),
                   label='larger_than_average')
    df.assert_that(small_but_nontrivial, df.equal_to(['bb']),
                   label='small_but_not_trivial')
    p.run()

  def test_pardo_side_input_dofn(self):
    words = ['a', 'bb', 'ccc', 'dddd']

    # [START model_pardo_side_input_dofn]
    class FilterUsingLength(df.DoFn):
      def process(self, context, lower_bound, upper_bound=float('inf')):
        if lower_bound <= len(context.element) <= upper_bound:
          yield context.element

    small_words = words | df.ParDo(FilterUsingLength(), 0, 3)
    # [END model_pardo_side_input_dofn]
    self.assertEqual({'a', 'bb', 'ccc'}, set(small_words))

  def test_pardo_with_side_outputs(self):
    # [START model_pardo_emitting_values_on_side_outputs]
    class ProcessWords(df.DoFn):

      def process(self, context, cutoff_length, marker):
        if len(context.element) <= cutoff_length:
          # Emit this short word to the main output.
          yield context.element
        else:
          # Emit this word's long length to a side output.
          yield pvalue.SideOutputValue(
              'above_cutoff_lengths', len(context.element))
        if context.element.startswith(marker):
          # Emit this word to a different side output.
          yield pvalue.SideOutputValue('marked strings', context.element)
    # [END model_pardo_emitting_values_on_side_outputs]

    words = ['a', 'an', 'the', 'music', 'xyz']

    # [START model_pardo_with_side_outputs]
    results = (words | df.ParDo(ProcessWords(), cutoff_length=2, marker='x')
                         .with_outputs('above_cutoff_lengths',
                                       'marked strings',
                                       main='below_cutoff_strings'))
    below = results.below_cutoff_strings
    above = results.above_cutoff_lengths
    marked = results['marked strings']  # indexing works as well
    # [END model_pardo_with_side_outputs]

    self.assertEqual({'a', 'an'}, set(below))
    self.assertEqual({3, 5}, set(above))
    self.assertEqual({'xyz'}, set(marked))

    # [START model_pardo_with_side_outputs_iter]
    below, above, marked = (words | df.ParDo(ProcessWords(), cutoff_length=2, marker='x')
                                      .with_outputs('above_cutoff_lengths',
                                                    'marked strings',
                                                    main='below_cutoff_strings'))
    # [END model_pardo_with_side_outputs_iter]

    self.assertEqual({'a', 'an'}, set(below))
    self.assertEqual({3, 5}, set(above))
    self.assertEqual({'xyz'}, set(marked))

  def test_pardo_with_undeclared_side_outputs(self):
    numbers = [1, 2, 3, 4, 5, 10, 20]
    # [START model_pardo_with_side_outputs_undeclared]
    def even_odd(x):
      yield pvalue.SideOutputValue('odd' if x % 2 else 'even', x)
      if x % 10 == 0:
        yield x

    results = numbers | df.FlatMap(even_odd).with_outputs()

    evens = results.even
    odds = results.odd
    tens = results[None]  # the undeclared main output
    # [END model_pardo_with_side_outputs_undeclared]

    self.assertEqual({2, 4, 10, 20}, set(evens))
    self.assertEqual({1, 3, 5}, set(odds))
    self.assertEqual({10, 20}, set(tens))


class TypeHintsTest(unittest.TestCase):

  def test_bad_types(self):
    p = df.Pipeline('DirectPipelineRunner',
                    options=PipelineOptions(sys.argv))

    # [START type_hints_missing_define_numbers]
    numbers = p | df.Create(['1', '2', '3'])
    # [START type_hints_missing_define_numbers]

    # Consider the following code.
    # [START type_hints_missing_apply]
    evens = numbers | df.Filter(lambda x: x % 2 == 0)
    # [END type_hints_missing_apply]

    # Now suppose numers was defined as [snippet above].
    # When running this pipeline, you'd get a runtime error,
    # possibly on a remote machine, possibly very late.

    with self.assertRaises(TypeError):
      p.run()

    # To catch this early, we can assert what types we expect.
    with self.assertRaises(typehints.TypeCheckError):
      # [START type_hints_takes]
      p.options.view_as(TypeOptions).pipeline_type_check = True
      evens = numbers | df.Filter(lambda x: x % 2 == 0).with_input_types(int)
      # [END type_hints_takes]

    # Type hints can be declared on DoFns and callables as well, rather
    # than where they're used, to be more self contained.
    with self.assertRaises(typehints.TypeCheckError):
      # [START type_hints_do_fn]
      @df.typehints.with_input_types(int)
      class FilterEvensDoFn(df.DoFn):
        def process(self, context):
          if context.element % 2 == 0:
            yield context.element
      evens = numbers | df.ParDo(FilterEvensDoFn())
      # [END type_hints_do_fn]

    words = p | df.Create('words', ['a', 'bb', 'c'])
    # One can assert outputs and apply them to transforms as well.
    # Helps document the contract and checks it at pipeline construction time.
    # [START type_hints_transform]
    T = df.typehints.TypeVariable('T')
    @df.typehints.with_input_types(T)
    @df.typehints.with_output_types(df.typehints.Tuple[int, T])
    class MyTransform(df.PTransform):
      def apply(self, pcoll):
        return pcoll | df.Map(lambda x: (len(x), x))

    words_with_lens = words | MyTransform()
    # [END type_hints_transform]

    with self.assertRaises(typehints.TypeCheckError):
      words_with_lens | df.Map(lambda x: x).with_input_types(
          df.typehints.Tuple[int, int])


class SnippetsTest(unittest.TestCase):

  def create_temp_file(self, contents=''):
    with tempfile.NamedTemporaryFile(delete=False) as f:
      f.write(contents)
      return f.name

  def get_output(self, path, sorted_output=True):
    lines = []
    with open(path) as f:
      lines = f.readlines()
    if sorted_output:
      return sorted(s.rstrip('\n') for s in lines)
    else:
      return [s.rstrip('\n') for s in lines]

  def test_model_pipelines(self):
    temp_path = self.create_temp_file('aa bb cc\n bb cc\n cc')
    result_path = temp_path + '.result'
    snippets.model_pipelines([
        '--input=%s*' % temp_path,
        '--output=%s' % result_path])
    self.assertEqual(
        self.get_output(result_path),
        [str(s) for s in [(u'aa', 1), (u'bb', 2), (u'cc', 3)]])

  def test_model_pcollection(self):
    temp_path = self.create_temp_file()
    snippets.model_pcollection(['--output=%s' % temp_path])
    self.assertEqual(self.get_output(temp_path, sorted_output=False), [
        'To be, or not to be: that is the question: ',
        'Whether \'tis nobler in the mind to suffer ',
        'The slings and arrows of outrageous fortune, ',
        'Or to take arms against a sea of troubles, '])

  def test_construct_pipeline(self):
    temp_path = self.create_temp_file(
        'abc def ghi\n jkl mno pqr\n stu vwx yz')
    result_path = self.create_temp_file()
    snippets.construct_pipeline({'read': temp_path, 'write': result_path})
    self.assertEqual(
        self.get_output(result_path),
        ['cba', 'fed', 'ihg', 'lkj', 'onm', 'rqp', 'uts', 'xwv', 'zy'])

  def test_model_textio(self):
    temp_path = self.create_temp_file('aa bb cc\n bb cc\n cc')
    result_path = temp_path + '.result'
    snippets.model_textio({'read': temp_path, 'write': result_path})
    self.assertEqual(
        ['aa', 'bb', 'bb', 'cc', 'cc', 'cc'],
        self.get_output(result_path))

  def test_model_bigqueryio(self):
    # We cannot test BigQueryIO functionality in unit tests therefore we limit
    # ourselves to making sure the pipeline containing BigQuery sources and
    # sinks can be built.
    self.assertEqual(None, snippets.model_bigqueryio())

  def test_model_composite_transform_example(self):
    contents = ['aa bb cc', 'bb cc', 'cc']
    result_path = self.create_temp_file()
    snippets.model_composite_transform_example(contents, result_path)
    self.assertEqual(['aa: 1', 'bb: 2', 'cc: 3'], self.get_output(result_path))

  def test_model_multiple_pcollections_flatten(self):
    contents = ['a', 'b', 'c', 'd', 'e', 'f']
    result_path = self.create_temp_file()
    snippets.model_multiple_pcollections_flatten(contents, result_path)
    self.assertEqual(contents, self.get_output(result_path))

  def test_model_multiple_pcollections_partition(self):
    contents = [17, 42, 64, 32, 0, 99, 53, 89]
    result_path = self.create_temp_file()
    snippets.model_multiple_pcollections_partition(contents, result_path)
    self.assertEqual(['0', '17', '32', '42', '53', '64', '89', '99'],
                     self.get_output(result_path))

  def test_model_group_by_key(self):
    contents = ['a bb ccc bb bb a']
    result_path = self.create_temp_file()
    snippets.model_group_by_key(contents, result_path)
    expected = [('a', 2), ('bb', 3), ('ccc', 1)]
    self.assertEqual([str(s) for s in expected], self.get_output(result_path))

  def test_model_co_group_by_key_tuple(self):
    email_list = [['a', 'a@example.com'], ['b', 'b@example.com']]
    phone_list = [['a', 'x4312'], ['b', 'x8452']]
    result_path = self.create_temp_file()
    snippets.model_co_group_by_key_tuple(email_list, phone_list, result_path)
    expect = ['a; a@example.com; x4312', 'b; b@example.com; x8452']
    self.assertEqual(expect, self.get_output(result_path))


class CombineTest(unittest.TestCase):
  """Tests for dataflow/model/combine."""

  def test_global_sum(self):
    pc = [1, 2, 3]
    # [START global_sum]
    result = pc | df.CombineGlobally(sum)
    # [END global_sum]
    self.assertEqual([6], result)

  def test_combine_values(self):
    occurences = [('cat', 1), ('cat', 5), ('cat', 9), ('dog', 5), ('dog', 2)]
    # [START combine_values]
    first_occurences = occurences | df.GroupByKey() | df.CombineValues(min)
    # [END combine_values]
    self.assertEqual({('cat', 1), ('dog', 2)}, set(first_occurences))

  def test_combine_per_key(self):
    player_accuracies = [
        ('cat', 1), ('cat', 5), ('cat', 9), ('cat', 1),
        ('dog', 5), ('dog', 2)]
    # [START combine_per_key]
    avg_accuracy_per_player = player_accuracies | df.CombinePerKey(df.combiners.MeanCombineFn())
    # [END combine_per_key]
    self.assertEqual({('cat', 4.0), ('dog', 3.5)}, set(avg_accuracy_per_player))

  def test_combine_concat(self):
    pc = ['a', 'b']
    # [START combine_concat]
    def concat(values, separator=', '):
      return separator.join(values)
    with_commas = pc | df.CombineGlobally(concat)
    with_dashes = pc | df.CombineGlobally(concat, separator='-')
    # [END combine_concat]
    self.assertEqual(1, len(with_commas))
    self.assertTrue(with_commas[0] in {'a, b', 'b, a'})
    self.assertEqual(1, len(with_dashes))
    self.assertTrue(with_dashes[0] in {'a-b', 'b-a'})

  def test_bounded_sum(self):
    # [START combine_bounded_sum]
    pc = [1, 10, 100, 1000]
    def bounded_sum(values, bound=500):
      return min(sum(values), bound)
    small_sum = pc | df.CombineGlobally(bounded_sum)              # [500]
    large_sum = pc | df.CombineGlobally(bounded_sum, bound=5000)  # [1111]
    # [END combine_bounded_sum]
    self.assertEqual([500], small_sum)
    self.assertEqual([1111], large_sum)

  def test_combine_reduce(self):
    factors = [2, 3, 5, 7]
    # [START combine_reduce]
    import functools
    import operator
    product = factors | df.CombineGlobally(functools.partial(reduce, operator.mul), 1)
    # [END combine_reduce]
    self.assertEqual([210], product)

  def test_custom_average(self):
    pc = [2, 3, 5, 7]


    # [START combine_custom_average]
    class AverageFn(df.CombineFn):
      def create_accumulator(self):
        return (0.0, 0)
      def add_input(self, (sum, count), input):
        return sum + input, count + 1
      def merge_accumulators(self, accumulators):
        sums, counts = zip(*accumulators)
        return sum(sums), sum(counts)
      def extract_output(self, (sum, count)):
        return sum / count if count else float('NaN')
    average = pc | df.CombineGlobally(AverageFn())
    # [END combine_custom_average]
    self.assertEqual([4.25], average)

  def test_keys(self):
    occurrences = [('cat', 1), ('cat', 5), ('dog', 5), ('cat', 9), ('dog', 2)]
    unique_keys = occurrences | snippets.Keys()
    self.assertEqual({'cat', 'dog'}, set(unique_keys))

  def test_count(self):
    occurrences = ['cat', 'dog', 'cat', 'cat', 'dog']
    perkey_counts = occurrences | snippets.Count()
    self.assertEqual({('cat', 3), ('dog', 2)}, set(perkey_counts))


if __name__ == '__main__':
  logging.getLogger().setLevel(logging.INFO)
  unittest.main()
