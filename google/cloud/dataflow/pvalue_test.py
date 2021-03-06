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

"""Unit tests for the PValue and PCollection classes."""

import unittest

from google.cloud.dataflow.pipeline import Pipeline
from google.cloud.dataflow.pvalue import PValue
from google.cloud.dataflow.transforms import PTransform


class FakePipeline(Pipeline):
  """Fake pipeline object used to check if apply() receives correct args."""

  def apply(self, *args, **kwargs):
    self.args = args
    self.kwargs = kwargs


class PValueTest(unittest.TestCase):

  def test_pvalue_expected_arguments(self):
    pipeline = Pipeline('DirectPipelineRunner')
    transform = PTransform()
    value = PValue(pipeline=pipeline, transform=transform)
    self.assertEqual(pipeline, value.pipeline)

  def test_pvalue_missing_arguments(self):
    self.assertRaises(ValueError, PValue,
                      pipeline=Pipeline('DirectPipelineRunner'))
    self.assertRaises(ValueError, PValue, transform=PTransform())


if __name__ == '__main__':
  unittest.main()
