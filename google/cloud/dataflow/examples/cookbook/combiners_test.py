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

"""Simple tests to showcase combiners.

The tests are meant to be "copy/paste" code snippets for the topic they address
(combiners in this case). Most examples use neither sources nor sinks.
The input data is generated simply with a Create transform and the output is
checked directly on the last PCollection produced.
"""

import logging
import unittest

import google.cloud.dataflow as df


class CombinersTest(unittest.TestCase):
  """Tests showcasing Dataflow combiners."""

  SAMPLE_DATA = [
      ('a', 1), ('b', 10), ('a', 2), ('a', 3), ('b', 20), ('c', 100)]

  def test_combine_per_key_with_callable(self):
    """CombinePerKey using a standard callable reducing iterables.

    A common case for Dataflow combiners is to sum (or max or min) over the
    values of each key. Such standard functions can be used directly as combiner
    functions. In fact, any function "reducing" an iterable to a single value
    can be used.
    """
    result = (
        df.Pipeline(runner=df.runners.DirectPipelineRunner())
        | df.Create(CombinersTest.SAMPLE_DATA)
        | df.CombinePerKey(sum))

    self.assertEqual(sorted(result.get()), [('a', 6), ('b', 30), ('c', 100)])

  def test_combine_per_key_with_custom_callable(self):
    """CombinePerKey using a custom function reducing iterables."""
    def multiply(values):
      result = 1
      for v in values:
        result *= v
      return result

    result = (
        df.Pipeline(runner=df.runners.DirectPipelineRunner())
        | df.Create(CombinersTest.SAMPLE_DATA)
        | df.CombinePerKey(multiply))

    self.assertEqual(sorted(result.get()), [('a', 6), ('b', 200), ('c', 100)])


if __name__ == '__main__':
  logging.getLogger().setLevel(logging.INFO)
  unittest.main()


