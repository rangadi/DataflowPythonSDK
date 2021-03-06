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

"""Core PTransform subclasses, such as FlatMap, GroupByKey, and Map."""

from __future__ import absolute_import

import copy
import logging
import uuid
import warnings

from google.cloud.dataflow import pvalue
from google.cloud.dataflow import typehints
from google.cloud.dataflow.coders import typecoders
from google.cloud.dataflow.internal import util
from google.cloud.dataflow.pvalue import AsIter
from google.cloud.dataflow.pvalue import AsSingleton
from google.cloud.dataflow.transforms import window
from google.cloud.dataflow.transforms.ptransform import PTransform
from google.cloud.dataflow.transforms.ptransform import ptransform_fn
from google.cloud.dataflow.transforms.ptransform import PTransformWithSideInputs
from google.cloud.dataflow.transforms.window import WindowedValue
from google.cloud.dataflow.transforms.window import WindowFn
from google.cloud.dataflow.typehints import Any
from google.cloud.dataflow.typehints import get_type_hints
from google.cloud.dataflow.typehints import is_consistent_with
from google.cloud.dataflow.typehints import Iterable
from google.cloud.dataflow.typehints import KV
from google.cloud.dataflow.typehints import trivial_inference
from google.cloud.dataflow.typehints import TypeCheckError
from google.cloud.dataflow.typehints import Union
from google.cloud.dataflow.typehints import WithTypeHints
from google.cloud.dataflow.typehints.trivial_inference import element_type
from google.cloud.dataflow.utils.options import TypeOptions


class DoFnProcessContext(object):
  """A processing context passed to DoFn methods during execution.

  Most importantly, a DoFn.process method will access context.element
  to get the element it is supposed to process.

  Attributes:
    label: label of the ParDo whose element is being processed.
    element: element being processed
      (in process method only; always None in start_bundle and finish_bundle)
    timestamp: timestamp of the element
      (in process method only; always None in start_bundle and finish_bundle)
    windows: windows of the element
      (in process method only; always None in start_bundle and finish_bundle)
    state: a DoFnState object, which holds the runner's internal state
      for this element.  For example, aggregator state is here.
      Not used by the pipeline code.
  """

  def __init__(self, label, element=None, state=None):
    """Initialize a processing context object with an element and state.

    The element represents one value from a PCollection that will be accessed
    by a DoFn object during pipeline execution, and state is an arbitrary object
    where counters and other pipeline state information can be passed in.

    DoFnProcessContext objects are also used as inputs to PartitionFn instances.

    Args:
      label: label of the PCollection whose element is being processed.
      element: element of a PCollection being processed using this context.
      state: a DoFnState object with state to be passed in to the DoFn object.
    """
    self.label = label
    self.state = state
    if element is not None:
      self.set_element(element)

  def set_element(self, windowed_value):
    if windowed_value is None:
      # Not currently processing an element.
      if hasattr(self, 'element'):
        del self.element
        del self.timestamp
        del self.windows
    else:
      self.element = windowed_value.value
      self.timestamp = windowed_value.timestamp
      self.windows = windowed_value.windows

  def aggregate_to(self, aggregator, input_value):
    """Provide a new input value for the aggregator.

    Args:
      aggregator: the aggregator to update
      input_value: the new value to input to the combine_fn of this aggregator.
    """
    self.state.counter_for(aggregator).update(input_value)


class DoFn(WithTypeHints):
  """A function object used by a transform with custom processing.

  The ParDo transform is such a transform. The ParDo.apply
  method will take an object of type DoFn and apply it to all elements of a
  PCollection object.

  In order to have concrete DoFn objects one has to subclass from DoFn and
  define the desired behavior (start_bundle/finish_bundle and process) or wrap a
  callable object using the CallableWrapperDoFn class.
  """

  def infer_output_type(self, input_type):
    # TODO(robertwb): Side inputs types.
    # TODO(robertwb): Assert compatibility with input type hint?
    return self._strip_output_annotations(
        trivial_inference.infer_return_type(self.process, [input_type]))

  def start_bundle(self, context, *args, **kwargs):
    """Called before a bundle of elements is processed on a worker.

    Elements to be processed are split into bundles and distributed
    to workers.  Before a worker calls process() on the first element
    of its bundle, it calls this method.

    Args:
      context: a DoFnProcessContext object
      *args: side inputs
      **kwargs: keyword side inputs

    """
    pass

  def finish_bundle(self, context, *args, **kwargs):
    """Called after a bundle of elements is processed on a worker.

    Args:
      context: a DoFnProcessContext object
      *args: side inputs
      **kwargs: keyword side inputs
    """
    pass

  def process(self, context, *args, **kwargs):
    """Called for each element of a pipeline.

    Args:
      context: a DoFnProcessContext object containing, among other
        attributes, the element to be processed.
        See the DoFnProcessContext documentation for details.
      *args: side inputs
      **kwargs: keyword side inputs
    """
    raise NotImplementedError

  @staticmethod
  def from_callable(fn):
    return CallableWrapperDoFn(fn)

  def process_argspec_fn(self):
    """Returns the Python callable that will eventually be invoked.

    This should ideally be the user-level function that is called with
    the main and (if any) side inputs, and is used to relate the type
    hint parameters with the input parameters (e.g., by argument name).
    """
    return self.process

  def _strip_output_annotations(self, type_hint):
    annotations = (window.TimestampedValue, window.WindowedValue,
                   pvalue.SideOutputValue)
    # TODO(robertwb): These should be parameterized types that the
    # type inferencer understands.
    if (type_hint in annotations
        or trivial_inference.element_type(type_hint) in annotations):
      return Any
    else:
      return type_hint


class CallableWrapperDoFn(DoFn):
  """A DoFn (function) object wrapping a callable object.

  The purpose of this class is to conveniently wrap simple functions and use
  them in transforms.
  """

  def __init__(self, fn):
    """Initializes a CallableWrapperDoFn object wrapping a callable.

    Args:
      fn: A callable object.

    Raises:
      TypeError: if fn parameter is not a callable type.
    """
    if not callable(fn):
      raise TypeError('Expected a callable object instead of: %r' % fn)

    self._fn = fn

    super(CallableWrapperDoFn, self).__init__()

  def __repr__(self):
    return 'CallableWrapperDoFn(%s)' % self._fn

  def default_type_hints(self):
    type_hints = get_type_hints(self._fn)
    # If the fn was a DoFn annotated with a type-hint that hinted a return
    # type compatible with Iterable[Any], then we strip off the outer
    # container type due to the 'flatten' portion of FlatMap.
    # TODO(robertwb): Should we require an iterable specification for FlatMap?
    if type_hints.output_types:
      args, kwargs = type_hints.output_types
      if len(args) == 1 and is_consistent_with(args[0], Iterable[Any]):
        type_hints = type_hints.copy()
        type_hints.set_output_types(element_type(args[0]), **kwargs)
    return type_hints

  def infer_output_type(self, input_type):
    return self._strip_output_annotations(
        trivial_inference.infer_return_type(self._fn, [input_type]))

  def process(self, context, *args, **kwargs):
    return self._fn(context.element, *args, **kwargs)

  def process_argspec_fn(self):
    return getattr(self._fn, '_argspec_fn', self._fn)


class CombineFn(WithTypeHints):
  """A function object used by a Combine transform with custom processing.

  A CombineFn specifies how multiple values in all or part of a PCollection can
  be merged into a single value---essentially providing the same kind of
  information as the arguments to the Python "reduce" builtin (except for the
  input argument, which is an instance of CombineFnProcessContext). The
  combining process proceeds as follows:

  1. Input values are partitioned into one or more batches.
  2. For each batch, the create_accumulator method is invoked to create a fresh
     initial "accumulator" value representing the combination of zero values.
  3. For each input value in the batch, the add_inputs method is invoked to
     combine more values with the accumulator for that batch.
  4. The merge_accumulators method is invoked to combine accumulators from
     separate batches into a single combined output accumulator value, once all
     of the accumulators have had all the input value in their batches added to
     them. This operation is invoked repeatedly, until there is only one
     accumulator value left.
  5. The extract_output operation is invoked on the final accumulator to get
     the output value.
  """

  def __init__(self):
    super(CombineFn, self).__init__()

  def create_accumulator(self, *args, **kwargs):
    """Return a fresh, empty accumulator for the combine operation.

    Args:
      *args: Additional arguments and side inputs.
      **kwargs: Additional arguments and side inputs.
    """
    raise NotImplementedError(str(self))

  def add_input(self, accumulator, element, *args, **kwargs):
    """Return result of folding element into accumulator.

    CombineFn implementors must override either add_input or add_inputs.

    Args:
      accumulator: the current accumulator
      element: the element to add
      *args: Additional arguments and side inputs.
      **kwargs: Additional arguments and side inputs.
    """
    raise NotImplementedError(str(self))

  def add_inputs(self, accumulator, elements, *args, **kwargs):
    """Returns the result of folding each element in elements into accumulator.

    This is provided in case the implementation affords more efficient
    bulk addition of elements. The default implementation simply loops
    over the inputs invoking add_input for each one.

    Args:
      accumulator: the current accumulator
      elements: the elements to add
      *args: Additional arguments and side inputs.
      **kwargs: Additional arguments and side inputs.
    """
    for element in elements:
      accumulator = self.add_input(accumulator, element, *args, **kwargs)
    return accumulator

  def merge_accumulators(self, accumulators, *args, **kwargs):
    """Returns the result of merging several accumulators
    to a single accumulator value.

    Args:
      accumulators: the accumulators to merge
      *args: Additional arguments and side inputs.
      **kwargs: Additional arguments and side inputs.
    """
    raise NotImplementedError(str(self))

  def extract_output(self, accumulator, *args, **kwargs):
    """Return result of converting accumulator into the output value.

    Args:
      accumulator: the final accumulator value computed by this CombineFn
        for the entire input key or PCollection.
      *args: Additional arguments and side inputs.
      **kwargs: Additional arguments and side inputs.
    """
    raise NotImplementedError(str(self))

  def apply(self, elements, *args, **kwargs):
    """Returns result of applying this CombineFn to the input values.

    Args:
      elements: the set of values to combine.
      *args: Additional arguments and side inputs.
      **kwargs: Additional arguments and side inputs.
    """
    return self.extract_output(
        self.add_inputs(
            self.create_accumulator(*args, **kwargs), elements,
            *args, **kwargs),
        *args, **kwargs)

  @staticmethod
  def from_callable(fn):
    return CallableWrapperCombineFn(fn)

  @staticmethod
  def maybe_from_callable(fn):
    return fn if isinstance(fn, CombineFn) else CallableWrapperCombineFn(fn)


class CallableWrapperCombineFn(CombineFn):
  """A CombineFn (function) object wrapping a callable object.

  The purpose of this class is to conveniently wrap simple functions and use
  them in Combine transforms.
  """
  _EMPTY = object()

  def __init__(self, fn):
    """Initializes a CallableFn object wrapping a callable.

    Args:
      fn: A callable object that reduces elements of an iterable to a single
        value (like the builtins sum and max). This callable must be capable of
        receiving the kind of values it generates as output in its input, and
        for best results, its operation must be commutative and associative.

    Raises:
      TypeError: if fn parameter is not a callable type.
    """
    if not callable(fn):
      raise TypeError('Expected a callable object instead of: %r' % fn)

    super(CallableWrapperCombineFn, self).__init__()
    self._fn = fn

  def __repr__(self):
    return "CallableWrapperCombineFn(%s)" % self._fn

  def create_accumulator(self, *args, **kwargs):
    return self._EMPTY

  def add_input(self, accumulator, element, *args, **kwargs):
    return self.add_inputs([element], *args, **kwargs)

  def add_inputs(self, accumulator, elements, *args, **kwargs):
    if accumulator is not self._EMPTY:
      def union():
        yield accumulator
        for e in elements:
          yield e
    else:
      union = lambda: elements
    return self._fn(union(), *args, **kwargs)

  def merge_accumulators(self, accumulators, *args, **kwargs):
    # It's (weakly) assumed that self._fn is associative.
    return self._fn(accumulators, *args, **kwargs)

  def extract_output(self, accumulator, *args, **kwargs):
    return self._fn(()) if accumulator is self._EMPTY else accumulator

  def default_type_hints(self):
    fn_hints = get_type_hints(self._fn)
    if fn_hints.input_types is None:
      return fn_hints
    else:
      # fn(Iterable[V]) -> V becomes CombineFn(V) -> V
      input_args, input_kwargs = fn_hints.input_types
      if not input_args:
        if len(input_kwargs) == 1:
          input_args, input_kwargs = tuple(input_kwargs.values()), {}
        else:
          raise TypeError('Combiner input type must be specified positionally.')
      if not is_consistent_with(input_args[0], Iterable[Any]):
        raise TypeCheckError(
            'All functions for a Combine PTransform must accept a '
            'single argument compatible with: Iterable[Any]. '
            'Instead a function with input type: %s was received.'
             % input_args[0])
      input_args = (element_type(input_args[0]),) + input_args[1:]
      # TODO(robertwb): Assert output type is consistent with input type?
      hints = fn_hints.copy()
      hints.set_input_types(*input_args, **input_kwargs)
      return hints


class PartitionFn(object):
  """A function object used by a Partition transform.

  A PartitionFn specifies how individual values in a PCollection will be placed
  into separate partitions, indexed by an integer.
  """

  def partition_for(self, context, num_partitions, *args, **kwargs):
    """Specify which partition will receive this element.

    Args:
      context: A DoFnProcessContext containing an element of the
        input PCollection.
      num_partitions: Number of partitions, i.e., output PCollections.
      *args: optional parameters and side inputs.
      **kwargs: optional parameters and side inputs.

    Returns:
      An integer in [0, num_partitions).
    """
    pass


class CallableWrapperPartitionFn(PartitionFn):
  """A PartitionFn object wrapping a callable object.

  Instances of this class wrap simple functions for use in Partition operations.
  """

  def __init__(self, fn):
    """Initializes a PartitionFn object wrapping a callable.

    Args:
      fn: A callable object, which should accept the following arguments:
            element - element to assign to a partition.
            num_partitions - number of output partitions.
          and may accept additional arguments and side inputs.

    Raises:
      TypeError: if fn is not a callable type.
    """
    if not callable(fn):
      raise TypeError('Expected a callable object instead of: %r' % fn)
    self._fn = fn

  def partition_for(self, context, num_partitions, *args, **kwargs):
    return self._fn(context.element, num_partitions, *args, **kwargs)


class ParDo(PTransformWithSideInputs):
  """A ParDo transform.

  Processes an input PCollection by applying a DoFn to each element and
  returning the accumulated results into an output PCollection. The type of the
  elements is not fixed as long as the DoFn can deal with it. In reality
  the type is restrained to some extent because the elements sometimes must be
  persisted to external storage. See the apply() method comments for a detailed
  description of all possible arguments.

  Note that the DoFn must return an iterable for each element of the input
  PCollection.  An easy way to do this is to use the yield keyword in the
  process method.

  Args:
      label: name of this transform instance. Useful while monitoring and
        debugging a pipeline execution.
      pcoll: a PCollection to be processed.
      dofn: a DoFn object to be applied to each element of pcoll argument.
      *args: positional arguments passed to the dofn object.
      **kwargs:  keyword arguments passed to the dofn object.

  Note that the positional and keyword arguments will be processed in order
  to detect PCollections that will be computed as side inputs to the
  transform. During pipeline execution whenever the DoFn object gets executed
  (its apply() method gets called) the PCollection arguments will be replaced
  by values from the PCollection in the exact positions where they appear in
  the argument lists.
  """

  def __init__(self, fn_or_label, *args, **kwargs):
    super(ParDo, self).__init__(fn_or_label, *args, **kwargs)

    if not isinstance(self.fn, DoFn):
      raise TypeError('ParDo must be called with a DoFn instance.')

  def default_type_hints(self):
    return self.fn.get_type_hints()

  def infer_output_type(self, input_type):
    return trivial_inference.element_type(
        self.fn.infer_output_type(input_type))

  def make_fn(self, fn):
    return fn if isinstance(fn, DoFn) else CallableWrapperDoFn(fn)

  def process_argspec_fn(self):
    return self.fn.process_argspec_fn()

  def apply(self, pcoll):
    self.side_output_tags = set()
    # TODO(robertwb): Change all uses of the dofn attribute to use fn instead.
    self.dofn = self.fn
    return pvalue.PCollection(pipeline=pcoll.pipeline, transform=self)

  def with_outputs(self, *tags, **main_kw):
    """Returns a tagged tuple allowing access to the outputs of a ParDo.

    The resulting object supports access to the
    PCollection associated with a tag (e.g., o.tag, o[tag]) and iterating over
    the available tags (e.g., for tag in o: ...).

    Args:
      *tags: if non-empty, list of valid tags
      **main_kw: dictionary empty or with one key 'main' defining the tag to be
        used for the main output (which will not have a tag associated with it).

    Returns:
      An object of type DoOutputsTuple that bundles together all the outputs
      of a ParDo transform and allows accessing the individual
      PCollections for each output using an object.tag syntax.

    Raises:
      TypeError: if the self object is not a PCollection that is the result of
        a ParDo transform.
      ValueError: if main_kw contains any key other than 'main'.
    """
    main_tag = main_kw.pop('main', None)
    if main_kw:
      raise ValueError('Unexpected keyword arguments: %s' % main_kw.keys())
    return _MultiParDo(self, tags, main_tag)


class _MultiParDo(PTransform):

  def __init__(self, do_transform, tags, main_tag):
    super(_MultiParDo, self).__init__(do_transform.label)
    self._do_transform = do_transform
    self._tags = tags
    self._main_tag = main_tag

  def apply(self, pcoll):
    _ = pcoll | self._do_transform
    return pvalue.DoOutputsTuple(
        pcoll.pipeline, self._do_transform, self._tags, self._main_tag)


def FlatMap(fn_or_label, *args, **kwargs):  # pylint: disable=invalid-name
  """FlatMap is like ParDo except it takes a callable to specify the
  transformation.

  The callable must return an iterable for each element of the input
  PCollection.  The elements of these iterables will be flattened into
  the output PCollection.

  Args:
    fn_or_label: name of this transform instance. Useful while monitoring and
      debugging a pipeline execution.
    *args: positional arguments passed to the transform callable.
    **kwargs: keyword arguments passed to the transform callable.

  Returns:
    A PCollection containing the Map outputs.

  Raises:
    TypeError: If the fn passed as argument is not a callable. Typical error
      is to pass a DoFn instance which is supported only for ParDo.
  """
  if fn_or_label is None or isinstance(fn_or_label, str):
    label, fn, args = fn_or_label, args[0], args[1:]
  else:
    label, fn = None, fn_or_label
  if not callable(fn):
    raise TypeError(
        'FlatMap can be used only with callable objects. '
        'Received %r instead for %s argument.'
        % (fn, 'first' if label is None else 'second'))

  return ParDo(label, CallableWrapperDoFn(fn), *args, **kwargs)


def Map(fn_or_label, *args, **kwargs):  # pylint: disable=invalid-name
  """Map is like FlatMap except its callable returns only a single element.

  Args:
    fn_or_label: name of this transform instance. Useful while monitoring and
      debugging a pipeline execution.
    *args: positional arguments passed to the transform callable.
    **kwargs: keyword arguments passed to the transform callable.

  Returns:
    A PCollection containing the Map outputs.

  Raises:
    TypeError: If the fn passed as argument is not a callable. Typical error
      is to pass a DoFn instance which is supported only for ParDo.
  """
  if isinstance(fn_or_label, str):
    label, fn, args = fn_or_label, args[0], args[1:]
  else:
    label, fn = None, fn_or_label
  if not callable(fn):
    raise TypeError(
        'Map can be used only with callable objects. '
        'Received %r instead for %s argument.'
        % (fn, 'first' if label is None else 'second'))
  wrapper = lambda x, *args, **kwargs: [fn(x, *args, **kwargs)]

  # Proxy the type-hint information from the original function to this new
  # wrapped function.
  get_type_hints(wrapper).input_types = get_type_hints(fn).input_types
  output_hint = get_type_hints(fn).simple_output_type(label)
  if output_hint:
    get_type_hints(wrapper).set_output_types(typehints.Iterable[output_hint])
  # pylint: disable=protected-access
  wrapper._argspec_fn = fn
  # pylint: enable=protected-access

  return FlatMap(label, wrapper, *args, **kwargs)


def Filter(fn_or_label, *args, **kwargs):  # pylint: disable=invalid-name
  """Filter is a FlatMap with its callable filtering out elements.

  Args:
    fn_or_label: name of this transform instance. Useful while monitoring and
      debugging a pipeline execution.
    *args: positional arguments passed to the transform callable.
    **kwargs: keyword arguments passed to the transform callable.

  Returns:
    A PCollection containing the Filter outputs.

  Raises:
    TypeError: If the fn passed as argument is not a callable. Typical error
      is to pass a DoFn instance which is supported only for FlatMap.
  """
  if isinstance(fn_or_label, str):
    label, fn, args = fn_or_label, args[0], args[1:]
  else:
    label, fn = None, fn_or_label
  if not callable(fn):
    raise TypeError(
        'Filter can be used only with callable objects. '
        'Received %r instead for %s argument.'
        % (fn, 'first' if label is None else 'second'))
  wrapper = lambda x, *args, **kwargs: [x] if fn(x, *args, **kwargs) else []

  # Proxy the type-hint information from the function being wrapped, setting the
  # output type to be the same as the input type.
  get_type_hints(wrapper).input_types = get_type_hints(fn).input_types
  output_hint = get_type_hints(fn).simple_output_type(label)
  if (output_hint is None
      and get_type_hints(wrapper).input_types
      and get_type_hints(wrapper).input_types[0]):
    output_hint = get_type_hints(wrapper).input_types[0]
  if output_hint:
    get_type_hints(wrapper).set_output_types(typehints.Iterable[output_hint])
  # pylint: disable=protected-access
  wrapper._argspec_fn = fn
  # pylint: enable=protected-access

  return FlatMap(label, wrapper, *args, **kwargs)


class CombineGlobally(PTransform):
  """A CombineGlobally transform.

  Reduces a PCollection to a single value by progressively applying a CombineFn
  to portions of the PCollection (and to intermediate values created thereby).
  See documentation in CombineFn for details on the specifics on how CombineFns
  are applied.

  Args:
    label: name of this transform instance. Useful while monitoring and
      debugging a pipeline execution.
    pcoll: a PCollection to be reduced into a single value.
    fn: a CombineFn object that will be called to progressively reduce the
      PCollection into single values, or a callable suitable for wrapping
      by CallableWrapperCombineFn.
    *args: positional arguments passed to the CombineFn object.
    **kwargs: keyword arguments passed to the CombineFn object.

  Raises:
    TypeError: If the output type of the input PCollection is not compatible
      with Iterable[A].

  Returns:
    A single-element PCollection containing the main output of the Combine
    transform.

  Note that the positional and keyword arguments will be processed in order
  to detect PObjects that will be computed as side inputs to the transform.
  During pipeline execution whenever the CombineFn object gets executed (i.e.,
  any of the CombineFn methods get called), the PObject arguments will be
  replaced by their actual value in the exact position where they appear in
  the argument lists.
  """
  has_defaults = True
  as_view = False

  def __init__(self, label_or_fn, *args, **kwargs):
    if label_or_fn is None or isinstance(label_or_fn, str):
      label, fn, args = label_or_fn, args[0], args[1:]
    else:
      label, fn = None, label_or_fn

    super(CombineGlobally, self).__init__(label)
    self.fn = fn
    self.args = args
    self.kwargs = kwargs

  def clone(self, **extra_attributes):
    clone = copy.copy(self)
    clone.__dict__.update(extra_attributes)
    return clone

  def with_defaults(self, has_defaults=True):
    return self.clone(has_defaults=has_defaults)

  def without_defaults(self):
    return self.with_defaults(False)

  def as_singleton_view(self):
    return self.clone(as_view=True)

  def apply(self, pcoll):
    def add_input_types(transform):
      type_hints = self.get_type_hints()
      if type_hints.input_types:
        return transform.with_input_types(type_hints.input_types[0][0])
      else:
        return transform

    combined = (pcoll
            | add_input_types(Map('KeyWithVoid', lambda v: (None, v))
               .with_output_types(KV[None, pcoll.element_type]))
            | CombinePerKey(self.fn, *self.args, **self.kwargs)
            | Map('UnKey', lambda (k, v): v))

    if not self.has_defaults and not self.as_view:
      return combined

    if self.has_defaults:
      combine_fn = (
          self.fn if isinstance(self.fn, CombineFn)
          else CombineFn.from_callable(self.fn))
      default_value = combine_fn.apply([], *self.args, **self.kwargs)
    else:
      default_value = pvalue.AsSingleton._NO_DEFAULT
    view = pvalue.AsSingleton(combined, default_value=default_value)
    if self.as_view:
      return view
    else:
      if pcoll.windowing.windowfn != window.GlobalWindows():
        raise ValueError(
            "Default values are not yet supported in CombineGlobally() if the "
            "output  PCollection is not windowed by GlobalWindows. "
            "Instead, use CombineGlobally().without_defaults() to output "
            "an empty PCollection if the input PCollection is empty, "
            "or CombineGlobally().as_singleton_view() to get the default "
            "output of the CombineFn if the input PCollection is empty.")
      def typed(transform):
        # TODO(robertwb): We should infer this.
        if combined.element_type:
          return transform.with_output_types(combined.element_type)
        else:
          return transform
      return (pcoll.pipeline
              | Create('DoOnce', [None])
              | typed(Map('InjectDefault', lambda _, s: s, view)))


def Combine(*args, **kwargs):  # pylint: disable=invalid-name
  warnings.warn('Combine is deprecated, use CombineGlobally instead',
                DeprecationWarning)
  return CombineGlobally(*args, **kwargs)


@ptransform_fn
def CombinePerKey(label, pcoll, fn, *args, **kwargs):  # pylint: disable=invalid-name
  """A per-key Combine transform.

  Identifies sets of values associated with the same key in the input
  PCollection, then applies a CombineFn to condense those sets to single
  values. See documentation in CombineFn for details on the specifics on how
  CombineFns are applied.

  Args:
    label: name of this transform instance. Useful while monitoring and
      debugging a pipeline execution.
    pcoll: input pcollection.
    fn: instance of CombineFn to apply to all values under the same key in
      pcoll, or a callable whose signature is f(iterable, *args, **kwargs)
      (e.g., sum, max).
    *args: arguments and side inputs, passed directly to the CombineFn.
    **kwargs: arguments and side inputs, passed directly to the CombineFn.

  Returns:
    A PObject holding the result of the combine operation.
  """
  return pcoll | GroupByKey() | CombineValues('Combine', fn, *args, **kwargs)


# TODO(robertwb): Rename to CombineGroupedValues?
class CombineValues(PTransformWithSideInputs):

  def make_fn(self, fn):
    return fn if isinstance(fn, CombineFn) else CombineFn.from_callable(fn)

  def apply(self, pcoll):
    args, kwargs = util.insert_values_in_args(
      self.args, self.kwargs, self.side_inputs)

    input_type = pcoll.element_type
    key_type = None
    if input_type is not None:
      key_type, _ = input_type.tuple_types

    runtime_type_check = (
        pcoll.pipeline.options is not None and
        pcoll.pipeline.options.view_as(TypeOptions).runtime_type_check)
    return pcoll | ParDo(
        CombineValuesDoFn(key_type, self.fn, runtime_type_check),
        *args, **kwargs)


class CombineValuesDoFn(DoFn):
  """DoFn for performing per-key Combine transforms."""

  def __init__(self, input_pcoll_type, combinefn, runtime_type_check):
    super(CombineValuesDoFn, self).__init__()
    self.combinefn = combinefn
    self.runtime_type_check = runtime_type_check

  def process(self, p_context, *args, **kwargs):
    # Expected elements input to this DoFn are 2-tuples of the form
    # (key, iter), with iter an iterable of all the values associated with key
    # in the input PCollection.
    if self.runtime_type_check:
      # Apply the combiner in a single operation rather than artificially
      # breaking it up so that output type violations manifest as TypeCheck
      # errors rather than type errors.
      return [
          (p_context.element[0],
           self.combinefn.apply(p_context.element[1], *args, **kwargs))]
    else:
      # Add the elements into three accumulators (for testing of merge).
      elements = p_context.element[1]
      accumulators = []
      for k in range(3):
        if len(elements) <= k:
          break
        accumulators.append(
            self.combinefn.add_inputs(
                self.combinefn.create_accumulator(*args, **kwargs),
                elements[k::3],
                *args, **kwargs))
      # Merge the accumulators.
      accumulator = self.combinefn.merge_accumulators(
          accumulators, *args, **kwargs)
      # Convert accumulator to the final result.
      return [(p_context.element[0],
               self.combinefn.extract_output(accumulator, *args, **kwargs))]

  def default_type_hints(self):
    hints = self.combinefn.get_type_hints().copy()
    if hints.input_types:
      K = typehints.TypeVariable('K')
      args, kwargs = hints.input_types
      args = (typehints.Tuple[K, typehints.Iterable[args[0]]],) + args[1:]
      hints.set_input_types(*args, **kwargs)
    else:
      K = typehints.Any
    if hints.output_types:
      main_output_type = hints.simple_output_type('')
      hints.set_output_types(typehints.Tuple[K, main_output_type])
    return hints


K = typehints.TypeVariable('K')
V = typehints.TypeVariable('V')
@typehints.with_input_types(typehints.KV[K, V])
@typehints.with_output_types(typehints.KV[K, typehints.Iterable[V]])
class GroupByKey(PTransform):
  """A group by key transform.

  Processes an input PCollection consisting of key/value pairs represented as a
  tuple pair. The result is a PCollection where values having a common key are
  grouped together.  For example (a, 1), (b, 2), (a, 3) will result into
  (a, [1, 3]), (b, [2]).

  The implementation here is used only when run on the local direct runner.
  """

  class ReifyWindows(DoFn):

    def process(self, context):
      try:
        k, v = context.element
      except TypeError:
        raise TypeCheckError('Input to GroupByKey must be a PCollection with '
                             'elements compatible with KV[A, B]')

      return [(k, window.WindowedValue(v, context.timestamp, context.windows))]

    def infer_output_type(self, input_type):
      key_type, value_type = trivial_inference.key_value_types(input_type)
      return Iterable[KV[key_type, typehints.WindowedValue[value_type]]]

  class GroupAlsoByWindow(DoFn):
    # TODO(robertwb): Support combiner lifting.

    def __init__(self, windowing):
      super(GroupByKey.GroupAlsoByWindow, self).__init__()
      self.windowing = windowing

    def infer_output_type(self, input_type):
      key_type, windowed_value_iter_type = trivial_inference.key_value_types(
          input_type)
      value_type = windowed_value_iter_type.inner_type.inner_type
      return Iterable[KV[key_type, Iterable[value_type]]]

    def process(self, context):
      k, vs = context.element
      # pylint: disable=g-import-not-at-top
      from google.cloud.dataflow.transforms.trigger import InMemoryUnmergedState
      from google.cloud.dataflow.transforms.trigger import create_trigger_driver
      # pylint: enable=g-import-not-at-top
      driver = create_trigger_driver(self.windowing, True)
      state = InMemoryUnmergedState()
      # TODO(robertwb): Conditionally process in smaller chunks.
      for out_window, values in driver.process_elements(vs, state):
        yield window.WindowedValue((k, values), out_window.end, [out_window])
      while state.timers:
        for timer_window, (tag, timestamp) in state.get_and_clear_timers():
          for out_window, values in driver.process_timer(timer_window,
                                                         timestamp, tag, state):
            yield window.WindowedValue(
                (k, values), out_window.end, [out_window])

  def apply(self, pcoll):
    # This code path is only used in the local direct runner.  For Dataflow
    # runner execution, the GroupByKey transform is expanded on the service.
    input_type = pcoll.element_type

    if input_type is not None:
      # Initialize type-hints used below to enforce type-checking and to pass
      # downstream to further PTransforms.
      key_type, value_type = trivial_inference.key_value_types(input_type)
      key_coder = typecoders.registry.get_coder(key_type)
      if not key_coder.is_deterministic():
        logging.warning('The key coder "%s" for the GroupByKey operation "%s" '
                        'is not deterministic. This may result in incorrect '
                        'pipeline output. This can be fixed by adding a type '
                        'hint to the operation preceding the GroupByKey step, '
                        'and for custom key classes, by writing a '
                        'deterministic custom Coder. Please see the '
                        'documentation for more details.',
                        key_coder, self.label)

      reify_output_type = KV[key_type, typehints.WindowedValue[value_type]]
      gbk_input_type = KV[key_type, Iterable[typehints.WindowedValue[value_type]]]
      gbk_output_type = KV[key_type, Iterable[value_type]]

      return (pcoll
              | (ParDo('reify_windows', self.ReifyWindows())
                 .with_output_types(reify_output_type))
              | (GroupByKeyOnly('group_by_key')
                 .with_input_types(reify_output_type)
                 .with_output_types(gbk_input_type))
              | (ParDo('group_by_window',
                       self.GroupAlsoByWindow(pcoll.windowing))
                 .with_input_types(gbk_input_type)
                 .with_output_types(gbk_output_type)))
    else:
      return (pcoll
              | ParDo('reify_windows', self.ReifyWindows())
              | GroupByKeyOnly('group_by_key')
              | ParDo('group_by_window',
                      self.GroupAlsoByWindow(pcoll.windowing)))


K = typehints.TypeVariable('K')
V = typehints.TypeVariable('V')
@typehints.with_input_types(typehints.KV[K, V])
@typehints.with_output_types(typehints.KV[K, typehints.Iterable[V]])
class GroupByKeyOnly(PTransform):
  """A group by key transform, ignoring windows."""

  def __init__(self, label=None):
    super(GroupByKeyOnly, self).__init__(label)

  def infer_output_type(self, input_type):
    key_type, value_type = trivial_inference.key_value_types(input_type)
    return KV[key_type, Iterable[value_type]]

  def apply(self, pcoll):
    self._check_pcollection(pcoll)
    return pvalue.PCollection(pipeline=pcoll.pipeline, transform=self)


class Partition(PTransformWithSideInputs):
  """Split a PCollection into several partitions.

  Uses the specified PartitionFn to separate an input PCollection into the
  specified number of sub-PCollections.

  When apply()d, a Partition() PTransform requires the following arguments:
    partitionfn: a PartitionFn, or a callable with the signature described in
      CallableWrapperPartitionFn.
    n: number of output partitions.

  The result of this PTransform is a simple list of the output PCollections
  representing each of n partitions, in order.
  """

  class ApplyPartitionFnFn(DoFn):
    """A DoFn that applies a PartitionFn."""

    def process(self, context, partitionfn, n, *args, **kwargs):
      partition = partitionfn.partition_for(context, n, *args, **kwargs)
      if not 0 <= partition < n:
        raise ValueError(
            'PartitionFn specified out-of-bounds partition index: '
            '%d not in [0, %d)' % (partition, n))
      # Each input is directed into the side output that corresponds to the
      # selected partition.
      yield pvalue.SideOutputValue(str(partition), context.element)

  def make_fn(self, fn):
    return fn if isinstance(fn, PartitionFn) else CallableWrapperPartitionFn(fn)

  def apply(self, pcoll):
    n = int(self.args[0])
    return pcoll | ParDo(
        self.ApplyPartitionFnFn(), self.fn, *self.args,
        **self.kwargs).with_outputs(*[str(t) for t in range(n)])


class Windowing(object):

  def __init__(self, windowfn, triggerfn=None, accumulation_mode=None):
    global AccumulationMode, DefaultTrigger
    # pylint: disable=g-import-not-at-top
    from google.cloud.dataflow.transforms.trigger import AccumulationMode, DefaultTrigger
    # pylint: enable=g-import-not-at-top
    if triggerfn is None:
      triggerfn = DefaultTrigger()
    if accumulation_mode is None:
      if triggerfn == DefaultTrigger():
        accumulation_mode = AccumulationMode.DISCARDING
      else:
        raise ValueError(
            'accumulation_mode must be provided for non-trivial triggers')
    self.windowfn = windowfn
    self.triggerfn = triggerfn
    self.accumulation_mode = accumulation_mode
    self._is_default = (
        self.windowfn == window.GlobalWindows() and
        self.triggerfn == DefaultTrigger() and
        self.accumulation_mode == AccumulationMode.DISCARDING)

  def __repr__(self):
    return "Windowing(%s, %s, %s)" % (self.windowfn, self.triggerfn,
                                      self.accumulation_mode)

  def is_default(self):
    return self._is_default


T = typehints.TypeVariable('T')
@typehints.with_input_types(T)
@typehints.with_output_types(T)
class WindowInto(ParDo):  # pylint: disable=g-wrong-blank-lines
  """A window transform assigning windows to each element of a PCollection.

  Transforms an input PCollection by applying a windowing function to each
  element.  Each transformed element in the result will be a WindowedValue
  element with the same input value and timestamp, with its new set of windows
  determined by the windowing function.
  """

  class WindowIntoFn(DoFn):
    """A DoFn that applies a WindowInto operation."""

    def __init__(self, windowing):
      self.windowing = windowing

    def process(self, context):
      context = WindowFn.AssignContext(context.timestamp,
                                       element=context.element,
                                       existing_windows=context.windows)
      new_windows = self.windowing.windowfn.assign(context)
      yield WindowedValue(context.element, context.timestamp, new_windows)

  def __init__(self, *args, **kwargs):
    """Initializes a WindowInto transform.

    Args:
      *args: A tuple of position arguments.
      **kwargs: A dictionary of keyword arguments.

    The *args, **kwargs are expected to be (label, windowfn) or (windowfn).
    The optional trigger and accumulation_mode kwargs may also be provided.
    """
    triggerfn = kwargs.pop('trigger', None)
    accumulation_mode = kwargs.pop('accumulation_mode', None)
    label, windowfn = self.parse_label_and_arg(args, kwargs, 'windowfn')
    self.windowing = Windowing(windowfn, triggerfn, accumulation_mode)
    dofn = self.WindowIntoFn(self.windowing)
    super(WindowInto, self).__init__(label, dofn)

  def get_windowing(self, unused_inputs):
    return self.windowing

  def infer_output_type(self, input_type):
    return input_type

  def apply(self, pcoll):
    input_type = pcoll.element_type

    if input_type is not None:
      output_type = input_type
      self.with_input_types(input_type)
      self.with_output_types(output_type)
    return super(WindowInto, self).apply(pcoll)


# Python's pickling is broken for nested classes.
WindowIntoFn = WindowInto.WindowIntoFn


class Flatten(PTransform):
  """Merges several PCollections into a single PCollection.

  Copies all elements in 0 or more PCollections into a single output
  PCollection. If there are no input PCollections, the resulting PCollection
  will be empty (but see also **kwargs below).

  Args:
    label: name of this transform instance. Useful while monitoring and
      debugging a pipeline execution.
    **kwargs: Accepts a single named argument "pipeline", which specifies the
      pipeline that "owns" this PTransform. Ordinarily Flatten can obtain this
      information from one of the input PCollections, but if there are none (or
      if there's a chance there may be none), this argument is the only way to
      provide pipeline information and should be considered mandatory.
  """

  def __init__(self, label=None, **kwargs):
    super(Flatten, self).__init__(label)
    self.pipeline = kwargs.pop('pipeline', None)
    if kwargs:
      raise ValueError('Unexpected keyword arguments: %s' % kwargs.keys())

  def _extract_input_pvalues(self, pvalueish):
    try:
      pvalueish = tuple(pvalueish)
    except TypeError:
      raise ValueError('Input to Flatten must be an iterable.')
    return pvalueish, pvalueish

  def apply(self, pcolls):
    for pcoll in pcolls:
      self._check_pcollection(pcoll)
    return pvalue.PCollection(pipeline=self.pipeline, transform=self)


class Create(PTransform):
  """A transform that creates a PCollection from an iterable."""

  def __init__(self, *args, **kwargs):
    """Initializes a Create transform.

    Args:
      *args: A tuple of position arguments.
      **kwargs: A dictionary of keyword arguments.

    The *args, **kwargs are expected to be (label, value) or (value).
    """
    label, value = self.parse_label_and_arg(args, kwargs, 'value')
    super(Create, self).__init__(label)
    if isinstance(value, basestring):
      raise TypeError('PTransform Create: Refusing to treat string as '
                      'an iterable. (string=%r)' % value)
    elif isinstance(value, dict):
      value = value.items()
    self.value = tuple(value)

  def infer_output_type(self, unused_input_type):
    if not self.value:
      return Any
    else:
      return Union[[trivial_inference.instance_to_type(v) for v in self.value]]

  def apply(self, pbegin):
    assert isinstance(pbegin, pvalue.PBegin)
    self.pipeline = pbegin.pipeline
    return pvalue.PCollection(pipeline=self.pipeline, transform=self)

  def get_windowing(self, unused_inputs):
    return Windowing(window.GlobalWindows())


def Read(*args, **kwargs):
  from google.cloud.dataflow import io
  return io.Read(*args, **kwargs)


def Write(*args, **kwargs):
  from google.cloud.dataflow import io
  return io.Write(*args, **kwargs)
