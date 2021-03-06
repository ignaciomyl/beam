#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# cython: profile=True

"""Worker operations executor.

For internal use only; no backwards-compatibility guarantees.
"""

# pytype: skip-file

from __future__ import absolute_import

import traceback
from builtins import next
from builtins import object
from builtins import zip
from typing import TYPE_CHECKING
from typing import Any
from typing import Dict
from typing import Iterable
from typing import List
from typing import Mapping
from typing import Optional
from typing import Tuple

from future.utils import raise_with_traceback
from past.builtins import unicode

from apache_beam.internal import util
from apache_beam.options.value_provider import RuntimeValueProvider
from apache_beam.pvalue import TaggedOutput
from apache_beam.transforms import DoFn
from apache_beam.transforms import core
from apache_beam.transforms import userstate
from apache_beam.transforms.core import RestrictionProvider
from apache_beam.transforms.window import GlobalWindow
from apache_beam.transforms.window import TimestampedValue
from apache_beam.transforms.window import WindowFn
from apache_beam.utils.counters import Counter
from apache_beam.utils.counters import CounterName
from apache_beam.utils.timestamp import Timestamp
from apache_beam.utils.windowed_value import WindowedValue

if TYPE_CHECKING:
  from apache_beam.io import iobase
  from apache_beam.transforms import sideinputs
  from apache_beam.transforms.core import TimerSpec


class NameContext(object):
  """Holds the name information for a step."""

  def __init__(self, step_name, transform_id=None):
    # type: (str, Optional[str]) -> None
    """Creates a new step NameContext.

    Args:
      step_name: The name of the step.
    """
    self.step_name = step_name
    self.transform_id = transform_id

  def __eq__(self, other):
    return self.step_name == other.step_name

  def __ne__(self, other):
    # TODO(BEAM-5949): Needed for Python 2 compatibility.
    return not self == other

  def __repr__(self):
    return 'NameContext(%s)' % self.__dict__

  def __hash__(self):
    return hash(self.step_name)

  def metrics_name(self):
    """Returns the step name used for metrics reporting."""
    return self.step_name

  def logging_name(self):
    """Returns the step name used for logging."""
    return self.step_name


# TODO(BEAM-4028): Move DataflowNameContext to Dataflow internal code.
class DataflowNameContext(NameContext):
  """Holds the name information for a step in Dataflow.

  This includes a step_name (e.g. s2), a user_name (e.g. Foo/Bar/ParDo(Fab)),
  and a system_name (e.g. s2-shuffle-read34)."""

  def __init__(self, step_name, user_name, system_name):
    """Creates a new step NameContext.

    Args:
      step_name: The internal name of the step (e.g. s2).
      user_name: The full user-given name of the step (e.g. Foo/Bar/ParDo(Far)).
      system_name: The step name in the optimized graph (e.g. s2-1).
    """
    super(DataflowNameContext, self).__init__(step_name)
    self.user_name = user_name
    self.system_name = system_name

  def __eq__(self, other):
    return (self.step_name == other.step_name and
            self.user_name == other.user_name and
            self.system_name == other.system_name)

  def __ne__(self, other):
    # TODO(BEAM-5949): Needed for Python 2 compatibility.
    return not self == other

  def __hash__(self):
    return hash((self.step_name, self.user_name, self.system_name))

  def __repr__(self):
    return 'DataflowNameContext(%s)' % self.__dict__

  def logging_name(self):
    """Stackdriver logging relies on user-given step names (e.g. Foo/Bar)."""
    return self.user_name


class Receiver(object):
  """For internal use only; no backwards-compatibility guarantees.

  An object that consumes a WindowedValue.

  This class can be efficiently used to pass values between the
  sdk and worker harnesses.
  """

  def receive(self, windowed_value):
    # type: (WindowedValue) -> None
    raise NotImplementedError


class MethodWrapper(object):
  """For internal use only; no backwards-compatibility guarantees.

  Represents a method that can be invoked by `DoFnInvoker`."""

  def __init__(self, obj_to_invoke, method_name):
    """
    Initiates a ``MethodWrapper``.

    Args:
      obj_to_invoke: the object that contains the method. Has to either be a
                    `DoFn` object or a `RestrictionProvider` object.
      method_name: name of the method as a string.
    """

    if not isinstance(obj_to_invoke, (DoFn, RestrictionProvider)):
      raise ValueError('\'obj_to_invoke\' has to be either a \'DoFn\' or '
                       'a \'RestrictionProvider\'. Received %r instead.'
                       % obj_to_invoke)

    self.args, self.defaults = core.get_function_arguments(obj_to_invoke,
                                                           method_name)

    # TODO(BEAM-5878) support kwonlyargs on Python 3.
    self.method_value = getattr(obj_to_invoke, method_name)

    self.has_userstate_arguments = False
    self.state_args_to_replace = {}  # type: Dict[str, core.StateSpec]
    self.timer_args_to_replace = {}  # type: Dict[str, core.TimerSpec]
    self.timestamp_arg_name = None  # type: Optional[str]
    self.window_arg_name = None  # type: Optional[str]
    self.key_arg_name = None  # type: Optional[str]
    self.restriction_provider = None
    self.restriction_provider_arg_name = None
    self.watermark_estimator = None
    self.watermark_estimator_arg_name = None

    for kw, v in zip(self.args[-len(self.defaults):], self.defaults):
      if isinstance(v, core.DoFn.StateParam):
        self.state_args_to_replace[kw] = v.state_spec
        self.has_userstate_arguments = True
      elif isinstance(v, core.DoFn.TimerParam):
        self.timer_args_to_replace[kw] = v.timer_spec
        self.has_userstate_arguments = True
      elif core.DoFn.TimestampParam == v:
        self.timestamp_arg_name = kw
      elif core.DoFn.WindowParam == v:
        self.window_arg_name = kw
      elif core.DoFn.KeyParam == v:
        self.key_arg_name = kw
      elif isinstance(v, core.DoFn.RestrictionParam):
        self.restriction_provider = v.restriction_provider
        self.restriction_provider_arg_name = kw
      elif isinstance(v, core.DoFn.WatermarkEstimatorParam):
        self.watermark_estimator = v.watermark_estimator
        self.watermark_estimator_arg_name = kw

  def invoke_timer_callback(self,
                            user_state_context,
                            key,
                            window,
                            timestamp):
    # TODO(ccy): support side inputs.
    kwargs = {}
    if self.has_userstate_arguments:
      for kw, state_spec in self.state_args_to_replace.items():
        kwargs[kw] = user_state_context.get_state(state_spec, key, window)
      for kw, timer_spec in self.timer_args_to_replace.items():
        kwargs[kw] = user_state_context.get_timer(timer_spec, key, window)

    if self.timestamp_arg_name:
      kwargs[self.timestamp_arg_name] = Timestamp(seconds=timestamp)
    if self.window_arg_name:
      kwargs[self.window_arg_name] = window
    if self.key_arg_name:
      kwargs[self.key_arg_name] = key

    if kwargs:
      return self.method_value(**kwargs)
    else:
      return self.method_value()


class DoFnSignature(object):
  """Represents the signature of a given ``DoFn`` object.

  Signature of a ``DoFn`` provides a view of the properties of a given ``DoFn``.
  Among other things, this will give an extensible way for for (1) accessing the
  structure of the ``DoFn`` including methods and method parameters
  (2) identifying features that a given ``DoFn`` support, for example, whether
  a given ``DoFn`` is a Splittable ``DoFn`` (
  https://s.apache.org/splittable-do-fn) (3) validating a ``DoFn`` based on the
  feature set offered by it.
  """

  def __init__(self, do_fn):
    # type: (core.DoFn) -> None
    # We add a property here for all methods defined by Beam DoFn features.

    assert isinstance(do_fn, core.DoFn)
    self.do_fn = do_fn

    self.process_method = MethodWrapper(do_fn, 'process')
    self.start_bundle_method = MethodWrapper(do_fn, 'start_bundle')
    self.finish_bundle_method = MethodWrapper(do_fn, 'finish_bundle')
    self.setup_lifecycle_method = MethodWrapper(do_fn, 'setup')
    self.teardown_lifecycle_method = MethodWrapper(do_fn, 'teardown')

    restriction_provider = self.get_restriction_provider()
    self.initial_restriction_method = (
        MethodWrapper(restriction_provider, 'initial_restriction')
        if restriction_provider else None)
    self.restriction_coder_method = (
        MethodWrapper(restriction_provider, 'restriction_coder')
        if restriction_provider else None)
    self.create_tracker_method = (
        MethodWrapper(restriction_provider, 'create_tracker')
        if restriction_provider else None)
    self.split_method = (
        MethodWrapper(restriction_provider, 'split')
        if restriction_provider else None)

    self._validate()

    # Handle stateful DoFns.
    self._is_stateful_dofn = userstate.is_stateful_dofn(do_fn)
    self.timer_methods = {}  # type: Dict[TimerSpec, MethodWrapper]
    if self._is_stateful_dofn:
      # Populate timer firing methods, keyed by TimerSpec.
      _, all_timer_specs = userstate.get_dofn_specs(do_fn)
      for timer_spec in all_timer_specs:
        method = timer_spec._attached_callback
        self.timer_methods[timer_spec] = MethodWrapper(do_fn, method.__name__)

  def get_restriction_provider(self):
    # type: () -> RestrictionProvider
    return self.process_method.restriction_provider

  def get_watermark_estimator(self):
    return self.process_method.watermark_estimator

  def _validate(self):
    self._validate_process()
    self._validate_bundle_method(self.start_bundle_method)
    self._validate_bundle_method(self.finish_bundle_method)
    self._validate_stateful_dofn()

  def _validate_process(self):
    """Validate that none of the DoFnParameters are repeated in the function
    """
    param_ids = [d.param_id for d in self.process_method.defaults
                 if isinstance(d, core._DoFnParam)]
    if len(param_ids) != len(set(param_ids)):
      raise ValueError(
          'DoFn %r has duplicate process method parameters: %s.' % (
              self.do_fn, param_ids))

  def _validate_bundle_method(self, method_wrapper):
    """Validate that none of the DoFnParameters are used in the function
    """
    for param in core.DoFn.DoFnProcessParams:
      if param in method_wrapper.defaults:
        raise ValueError(
            'DoFn.process() method-only parameter %s cannot be used in %s.' %
            (param, method_wrapper))

  def _validate_stateful_dofn(self):
    userstate.validate_stateful_dofn(self.do_fn)

  def is_splittable_dofn(self):
    # type: () -> bool
    return self.get_restriction_provider() is not None

  def is_stateful_dofn(self):
    # type: () -> bool
    return self._is_stateful_dofn

  def has_timers(self):
    # type: () -> bool
    _, all_timer_specs = userstate.get_dofn_specs(self.do_fn)
    return bool(all_timer_specs)


class DoFnInvoker(object):
  """An abstraction that can be used to execute DoFn methods.

  A DoFnInvoker describes a particular way for invoking methods of a DoFn
  represented by a given DoFnSignature."""

  def __init__(self,
               output_processor,  # type: Optional[_OutputProcessor]
               signature  # type: DoFnSignature
              ):
    # type: (...) -> None
    """
    Initializes `DoFnInvoker`

    :param output_processor: an OutputProcessor for receiving elements produced
                             by invoking functions of the DoFn.
    :param signature: a DoFnSignature for the DoFn being invoked
    """
    self.output_processor = output_processor
    self.signature = signature
    self.user_state_context = None  # type: Optional[userstate.UserStateContext]
    self.bundle_finalizer_param = None  # type: Optional[core._BundleFinalizerParam]

  @staticmethod
  def create_invoker(
      signature,  # type: DoFnSignature
      output_processor=None,  # type: Optional[_OutputProcessor]
      context=None,  # type: Optional[DoFnContext]
      side_inputs=None,   # type: Optional[List[sideinputs.SideInputMap]]
      input_args=None, input_kwargs=None,
      process_invocation=True,
      user_state_context=None,  # type: Optional[userstate.UserStateContext]
      bundle_finalizer_param=None  # type: Optional[core._BundleFinalizerParam]
  ):
    # type: (...) -> DoFnInvoker
    """ Creates a new DoFnInvoker based on given arguments.

    Args:
        output_processor: an OutputProcessor for receiving elements produced by
                          invoking functions of the DoFn.
        signature: a DoFnSignature for the DoFn being invoked.
        context: Context to be used when invoking the DoFn (deprecated).
        side_inputs: side inputs to be used when invoking th process method.
        input_args: arguments to be used when invoking the process method. Some
                    of the arguments given here might be placeholders (for
                    example for side inputs) that get filled before invoking the
                    process method.
        input_kwargs: keyword arguments to be used when invoking the process
                      method. Some of the keyword arguments given here might be
                      placeholders (for example for side inputs) that get filled
                      before invoking the process method.
        process_invocation: If True, this function may return an invoker that
                            performs extra optimizations for invoking process()
                            method efficiently.
        user_state_context: The UserStateContext instance for the current
                            Stateful DoFn.
        bundle_finalizer_param: The param that passed to a process method, which
                                allows a callback to be registered.
    """
    side_inputs = side_inputs or []
    default_arg_values = signature.process_method.defaults
    use_simple_invoker = not process_invocation or (
        not side_inputs and not input_args and not input_kwargs and
        not default_arg_values and not signature.is_stateful_dofn())
    if use_simple_invoker:
      return SimpleInvoker(output_processor, signature)
    else:
      return PerWindowInvoker(
          output_processor,
          signature, context, side_inputs, input_args, input_kwargs,
          user_state_context, bundle_finalizer_param)

  def invoke_process(self,
                     windowed_value,  # type: WindowedValue
                     restriction_tracker=None,  # type: Optional[iobase.RestrictionTracker]
                     output_processor=None,  # type: Optional[OutputProcessor]
                     additional_args=None,
                     additional_kwargs=None
                    ):
    # type: (...) -> Optional[Tuple[WindowedValue, Timestamp]]
    """Invokes the DoFn.process() function.

    Args:
      windowed_value: a WindowedValue object that gives the element for which
                      process() method should be invoked along with the window
                      the element belongs to.
      output_procesor: if provided given OutputProcessor will be used.
      additional_args: additional arguments to be passed to the current
                      `DoFn.process()` invocation, usually as side inputs.
      additional_kwargs: additional keyword arguments to be passed to the
                         current `DoFn.process()` invocation.
    """
    raise NotImplementedError

  def invoke_setup(self):
    # type: () -> None
    """Invokes the DoFn.setup() method
    """
    self.signature.setup_lifecycle_method.method_value()

  def invoke_start_bundle(self):
    # type: () -> None
    """Invokes the DoFn.start_bundle() method.
    """
    self.output_processor.start_bundle_outputs(
        self.signature.start_bundle_method.method_value())

  def invoke_finish_bundle(self):
    # type: () -> None
    """Invokes the DoFn.finish_bundle() method.
    """
    self.output_processor.finish_bundle_outputs(
        self.signature.finish_bundle_method.method_value())

  def invoke_teardown(self):
    # type: () -> None
    """Invokes the DoFn.teardown() method
    """
    self.signature.teardown_lifecycle_method.method_value()

  def invoke_user_timer(self, timer_spec, key, window, timestamp):
    self.output_processor.process_outputs(
        WindowedValue(None, timestamp, (window,)),
        self.signature.timer_methods[timer_spec].invoke_timer_callback(
            self.user_state_context, key, window, timestamp))

  def invoke_split(self, element, restriction):
    return self.signature.split_method.method_value(element, restriction)

  def invoke_initial_restriction(self, element):
    return self.signature.initial_restriction_method.method_value(element)

  def invoke_restriction_coder(self):
    return self.signature.restriction_coder_method.method_value()

  def invoke_create_tracker(self, restriction):
    return self.signature.create_tracker_method.method_value(restriction)


class SimpleInvoker(DoFnInvoker):
  """An invoker that processes elements ignoring windowing information."""

  def __init__(self,
               output_processor,  # type: Optional[_OutputProcessor]
               signature  # type: DoFnSignature
              ):
    # type: (...) -> None
    super(SimpleInvoker, self).__init__(output_processor, signature)
    self.process_method = signature.process_method.method_value

  def invoke_process(self,
                     windowed_value,  # type: WindowedValue
                     restriction_tracker=None,  # type: Optional[iobase.RestrictionTracker]
                     output_processor=None,  # type: Optional[OutputProcessor]
                     additional_args=None,
                     additional_kwargs=None
                    ):
    # type: (...) -> None
    if not output_processor:
      output_processor = self.output_processor
    output_processor.process_outputs(
        windowed_value, self.process_method(windowed_value.value))


class PerWindowInvoker(DoFnInvoker):
  """An invoker that processes elements considering windowing information."""

  def __init__(self,
               output_processor,  # type: Optional[_OutputProcessor]
               signature,  # type: DoFnSignature
               context,  # type: DoFnContext
               side_inputs,  # type: Iterable[sideinputs.SideInputMap]
               input_args,
               input_kwargs,
               user_state_context,  # type: Optional[userstate.UserStateContext]
               bundle_finalizer_param  # type: Optional[core._BundleFinalizerParam]
              ):
    super(PerWindowInvoker, self).__init__(output_processor, signature)
    self.side_inputs = side_inputs
    self.context = context
    self.process_method = signature.process_method.method_value
    default_arg_values = signature.process_method.defaults
    self.has_windowed_inputs = (
        not all(si.is_globally_windowed() for si in side_inputs) or
        (core.DoFn.WindowParam in default_arg_values) or
        signature.is_stateful_dofn())
    self.user_state_context = user_state_context
    self.is_splittable = signature.is_splittable_dofn()
    self.watermark_estimator = self.signature.get_watermark_estimator()
    self.watermark_estimator_param = (
        self.signature.process_method.watermark_estimator_arg_name
        if self.watermark_estimator else None)
    self.threadsafe_restriction_tracker = None
    self.current_windowed_value = None
    self.bundle_finalizer_param = bundle_finalizer_param
    self.is_key_param_required = False

    # Try to prepare all the arguments that can just be filled in
    # without any additional work. in the process function.
    # Also cache all the placeholders needed in the process function.

    # Flag to cache additional arguments on the first element if all
    # inputs are within the global window.
    self.cache_globally_windowed_args = not self.has_windowed_inputs

    input_args = input_args if input_args else []
    input_kwargs = input_kwargs if input_kwargs else {}

    arg_names = signature.process_method.args

    # Create placeholder for element parameter of DoFn.process() method.
    # Not to be confused with ArgumentPlaceHolder, which may be passed in
    # input_args and is a placeholder for side-inputs.
    class ArgPlaceholder(object):
      def __init__(self, placeholder):
        self.placeholder = placeholder

    if core.DoFn.ElementParam not in default_arg_values:
      # TODO(BEAM-7867): Handle cases in which len(arg_names) ==
      #   len(default_arg_values).
      args_to_pick = len(arg_names) - len(default_arg_values) - 1
      # Positional argument values for process(), with placeholders for special
      # values such as the element, timestamp, etc.
      args_with_placeholders = (
          [ArgPlaceholder(core.DoFn.ElementParam)] + input_args[:args_to_pick])
    else:
      args_to_pick = len(arg_names) - len(default_arg_values)
      args_with_placeholders = input_args[:args_to_pick]

    # Fill the OtherPlaceholders for context, key, window or timestamp
    remaining_args_iter = iter(input_args[args_to_pick:])
    for a, d in zip(arg_names[-len(default_arg_values):], default_arg_values):
      if core.DoFn.ElementParam == d:
        args_with_placeholders.append(ArgPlaceholder(d))
      elif core.DoFn.KeyParam == d:
        self.is_key_param_required = True
        args_with_placeholders.append(ArgPlaceholder(d))
      elif core.DoFn.WindowParam == d:
        args_with_placeholders.append(ArgPlaceholder(d))
      elif core.DoFn.TimestampParam == d:
        args_with_placeholders.append(ArgPlaceholder(d))
      elif core.DoFn.PaneInfoParam == d:
        args_with_placeholders.append(ArgPlaceholder(d))
      elif core.DoFn.SideInputParam == d:
        # If no more args are present then the value must be passed via kwarg
        try:
          args_with_placeholders.append(next(remaining_args_iter))
        except StopIteration:
          if a not in input_kwargs:
            raise ValueError("Value for sideinput %s not provided" % a)
      elif isinstance(d, core.DoFn.StateParam):
        args_with_placeholders.append(ArgPlaceholder(d))
      elif isinstance(d, core.DoFn.TimerParam):
        args_with_placeholders.append(ArgPlaceholder(d))
      elif isinstance(d, type) and core.DoFn.BundleFinalizerParam == d:
        args_with_placeholders.append(ArgPlaceholder(d))
      else:
        # If no more args are present then the value must be passed via kwarg
        try:
          args_with_placeholders.append(next(remaining_args_iter))
        except StopIteration:
          pass
    args_with_placeholders.extend(list(remaining_args_iter))

    # Stash the list of placeholder positions for performance
    self.placeholders = [(i, x.placeholder) for (i, x) in enumerate(
        args_with_placeholders)
                         if isinstance(x, ArgPlaceholder)]

    self.args_for_process = args_with_placeholders
    self.kwargs_for_process = input_kwargs

  def invoke_process(self,
                     windowed_value,  # type: WindowedValue
                     restriction_tracker=None,
                     output_processor=None,  # type: Optional[OutputProcessor]
                     additional_args=None,
                     additional_kwargs=None
                    ):
    # type: (...) -> Optional[Tuple[WindowedValue, Timestamp]]
    if not additional_args:
      additional_args = []
    if not additional_kwargs:
      additional_kwargs = {}

    if not output_processor:
      output_processor = self.output_processor
    self.context.set_element(windowed_value)
    # Call for the process function for each window if has windowed side inputs
    # or if the process accesses the window parameter. We can just call it once
    # otherwise as none of the arguments are changing

    if self.is_splittable and not restriction_tracker:
      restriction = self.invoke_initial_restriction(windowed_value.value)
      restriction_tracker = self.invoke_create_tracker(restriction)

    if restriction_tracker:
      if len(windowed_value.windows) > 1 and self.has_windowed_inputs:
        # Should never get here due to window explosion in
        # the upstream pair-with-restriction.
        raise NotImplementedError(
            'SDFs in multiply-windowed values with windowed arguments.')
      restriction_tracker_param = (
          self.signature.process_method.restriction_provider_arg_name)
      if not restriction_tracker_param:
        raise ValueError(
            'A RestrictionTracker %r was provided but DoFn does not have a '
            'RestrictionTrackerParam defined' % restriction_tracker)
      from apache_beam.io import iobase
      self.threadsafe_restriction_tracker = iobase.ThreadsafeRestrictionTracker(
          restriction_tracker)
      additional_kwargs[restriction_tracker_param] = (
          iobase.RestrictionTrackerView(self.threadsafe_restriction_tracker))

      if self.watermark_estimator:
        # The watermark estimator needs to be reset for every element.
        self.watermark_estimator.reset()
        additional_kwargs[self.watermark_estimator_param] = (
            self.watermark_estimator)
      try:
        self.current_windowed_value = windowed_value
        return self._invoke_process_per_window(
            windowed_value, additional_args, additional_kwargs,
            output_processor)
      finally:
        self.threadsafe_restriction_tracker = None
        self.current_windowed_value = windowed_value

    elif self.has_windowed_inputs and len(windowed_value.windows) != 1:
      for w in windowed_value.windows:
        self._invoke_process_per_window(
            WindowedValue(windowed_value.value, windowed_value.timestamp, (w,)),
            additional_args, additional_kwargs, output_processor)
    else:
      self._invoke_process_per_window(
          windowed_value, additional_args, additional_kwargs, output_processor)

  def _invoke_process_per_window(self,
                                 windowed_value,  # type: WindowedValue
                                 additional_args,
                                 additional_kwargs,
                                 output_processor  # type: OutputProcessor
                                ):
    # type: (...) -> Optional[Tuple[WindowedValue, Timestamp]]
    if self.has_windowed_inputs:
      window, = windowed_value.windows
      side_inputs = [si[window] for si in self.side_inputs]
      side_inputs.extend(additional_args)
      args_for_process, kwargs_for_process = util.insert_values_in_args(
          self.args_for_process, self.kwargs_for_process,
          side_inputs)
    elif self.cache_globally_windowed_args:
      # Attempt to cache additional args if all inputs are globally
      # windowed inputs when processing the first element.
      self.cache_globally_windowed_args = False

      # Fill in sideInputs if they are globally windowed
      global_window = GlobalWindow()
      self.args_for_process, self.kwargs_for_process = (
          util.insert_values_in_args(
              self.args_for_process, self.kwargs_for_process,
              [si[global_window] for si in self.side_inputs]))
      args_for_process, kwargs_for_process = (
          self.args_for_process, self.kwargs_for_process)
    else:
      args_for_process, kwargs_for_process = (
          self.args_for_process, self.kwargs_for_process)

    # Extract key in the case of a stateful DoFn. Note that in the case of a
    # stateful DoFn, we set during __init__ self.has_windowed_inputs to be
    # True. Therefore, windows will be exploded coming into this method, and
    # we can rely on the window variable being set above.
    if self.user_state_context or self.is_key_param_required:
      try:
        key, unused_value = windowed_value.value
      except (TypeError, ValueError):
        raise ValueError(
            ('Input value to a stateful DoFn or KeyParam must be a KV tuple; '
             'instead, got \'%s\'.') % (windowed_value.value,))

    for i, p in self.placeholders:
      if core.DoFn.ElementParam == p:
        args_for_process[i] = windowed_value.value
      elif core.DoFn.KeyParam == p:
        args_for_process[i] = key
      elif core.DoFn.WindowParam == p:
        args_for_process[i] = window
      elif core.DoFn.TimestampParam == p:
        args_for_process[i] = windowed_value.timestamp
      elif core.DoFn.PaneInfoParam == p:
        args_for_process[i] = windowed_value.pane_info
      elif isinstance(p, core.DoFn.StateParam):
        args_for_process[i] = (
            self.user_state_context.get_state(p.state_spec, key, window))
      elif isinstance(p, core.DoFn.TimerParam):
        args_for_process[i] = (
            self.user_state_context.get_timer(p.timer_spec, key, window))
      elif core.DoFn.BundleFinalizerParam == p:
        args_for_process[i] = self.bundle_finalizer_param

    if additional_kwargs:
      if kwargs_for_process is None:
        kwargs_for_process = additional_kwargs
      else:
        for key in additional_kwargs:
          kwargs_for_process[key] = additional_kwargs[key]

    if kwargs_for_process:
      output_processor.process_outputs(
          windowed_value,
          self.process_method(*args_for_process, **kwargs_for_process))
    else:
      output_processor.process_outputs(
          windowed_value, self.process_method(*args_for_process))

    if self.is_splittable:
      # TODO: Consider calling check_done right after SDF.Process() finishing.
      # In order to do this, we need to know that current invoking dofn is
      # ProcessSizedElementAndRestriction.
      self.threadsafe_restriction_tracker.check_done()
      deferred_status = self.threadsafe_restriction_tracker.deferred_status()
      output_watermark = None
      if self.watermark_estimator:
        output_watermark = self.watermark_estimator.current_watermark()
      if deferred_status:
        deferred_restriction, deferred_watermark = deferred_status
        element = windowed_value.value
        size = self.signature.get_restriction_provider().restriction_size(
            element, deferred_restriction)
        return ((
            windowed_value.with_value(((element, deferred_restriction), size)),
            output_watermark), deferred_watermark)

  def try_split(self, fraction):
    restriction_tracker = self.threadsafe_restriction_tracker
    current_windowed_value = self.current_windowed_value
    if restriction_tracker and current_windowed_value:
      # Temporary workaround for [BEAM-7473]: get current_watermark before
      # split, in case watermark gets advanced before getting split results.
      # In worst case, current_watermark is always stale, which is ok.
      if self.watermark_estimator:
        current_watermark = self.watermark_estimator.current_watermark()
      else:
        current_watermark = None
      split = restriction_tracker.try_split(fraction)
      if split:
        primary, residual = split
        element = self.current_windowed_value.value
        restriction_provider = self.signature.get_restriction_provider()
        primary_size = restriction_provider.restriction_size(element, primary)
        residual_size = restriction_provider.restriction_size(element, residual)
        return (
            ((self.current_windowed_value.with_value((
                (element, primary), primary_size)), None), None),
            ((self.current_windowed_value.with_value((
                (element, residual), residual_size)), current_watermark), None))

  def current_element_progress(self):
    # type: () -> Optional[iobase.RestrictionProgress]
    restriction_tracker = self.threadsafe_restriction_tracker
    if restriction_tracker:
      return restriction_tracker.current_progress()


class DoFnRunner(Receiver):
  """For internal use only; no backwards-compatibility guarantees.

  A helper class for executing ParDo operations.
  """

  def __init__(self,
               fn,  # type: core.DoFn
               args,
               kwargs,
               side_inputs,  # type: Iterable[sideinputs.SideInputMap]
               windowing,
               tagged_receivers=None,  # type: Mapping[Optional[str], Receiver]
               step_name=None,  # type: Optional[str]
               logging_context=None,
               state=None,
               scoped_metrics_container=None,
               operation_name=None,
               user_state_context=None  # type: Optional[userstate.UserStateContext]
              ):
    """Initializes a DoFnRunner.

    Args:
      fn: user DoFn to invoke
      args: positional side input arguments (static and placeholder), if any
      kwargs: keyword side input arguments (static and placeholder), if any
      side_inputs: list of sideinput.SideInputMaps for deferred side inputs
      windowing: windowing properties of the output PCollection(s)
      tagged_receivers: a dict of tag name to Receiver objects
      step_name: the name of this step
      logging_context: DEPRECATED [BEAM-4728]
      state: handle for accessing DoFn state
      scoped_metrics_container: DEPRECATED
      operation_name: The system name assigned by the runner for this operation.
      user_state_context: The UserStateContext instance for the current
                          Stateful DoFn.
    """
    # Need to support multiple iterations.
    side_inputs = list(side_inputs)

    self.step_name = step_name
    self.context = DoFnContext(step_name, state=state)
    self.bundle_finalizer_param = DoFn.BundleFinalizerParam()

    do_fn_signature = DoFnSignature(fn)

    # Optimize for the common case.
    main_receivers = tagged_receivers[None]

    # TODO(BEAM-3937): Remove if block after output counter released.
    if 'outputs_per_element_counter' in RuntimeValueProvider.experiments:
      # TODO(BEAM-3955): Make step_name and operation_name less confused.
      output_counter_name = (CounterName('per-element-output-count',
                                         step_name=operation_name))
      per_element_output_counter = state._counter_factory.get_counter(
          output_counter_name, Counter.DATAFLOW_DISTRIBUTION).accumulator
    else:
      per_element_output_counter = None

    output_processor = _OutputProcessor(
        windowing.windowfn, main_receivers, tagged_receivers,
        per_element_output_counter)

    if do_fn_signature.is_stateful_dofn() and not user_state_context:
      raise Exception(
          'Requested execution of a stateful DoFn, but no user state context '
          'is available. This likely means that the current runner does not '
          'support the execution of stateful DoFns.')

    self.do_fn_invoker = DoFnInvoker.create_invoker(
        do_fn_signature, output_processor, self.context, side_inputs, args,
        kwargs, user_state_context=user_state_context,
        bundle_finalizer_param=self.bundle_finalizer_param)

  def receive(self, windowed_value):
    # type: (WindowedValue) -> None
    self.process(windowed_value)

  def process(self, windowed_value):
    # type: (WindowedValue) -> Optional[Tuple[WindowedValue, Timestamp]]
    try:
      return self.do_fn_invoker.invoke_process(windowed_value)
    except BaseException as exn:
      self._reraise_augmented(exn)

  def process_with_sized_restriction(self, windowed_value):
    # type: (WindowedValue) -> Optional[Tuple[WindowedValue, Timestamp]]
    (element, restriction), _ = windowed_value.value
    return self.do_fn_invoker.invoke_process(
        windowed_value.with_value(element),
        restriction_tracker=self.do_fn_invoker.invoke_create_tracker(
            restriction))

  def try_split(self, fraction):
    return self.do_fn_invoker.try_split(fraction)

  def current_element_progress(self):
    # type: () -> Optional[iobase.RestrictionProgress]
    return self.do_fn_invoker.current_element_progress()

  def process_user_timer(self, timer_spec, key, window, timestamp):
    try:
      self.do_fn_invoker.invoke_user_timer(timer_spec, key, window, timestamp)
    except BaseException as exn:
      self._reraise_augmented(exn)

  def _invoke_bundle_method(self, bundle_method):
    try:
      self.context.set_element(None)
      bundle_method()
    except BaseException as exn:
      self._reraise_augmented(exn)

  def _invoke_lifecycle_method(self, lifecycle_method):
    try:
      self.context.set_element(None)
      lifecycle_method()
    except BaseException as exn:
      self._reraise_augmented(exn)

  def setup(self):
    self._invoke_lifecycle_method(self.do_fn_invoker.invoke_setup)

  def start(self):
    self._invoke_bundle_method(self.do_fn_invoker.invoke_start_bundle)

  def finish(self):
    self._invoke_bundle_method(self.do_fn_invoker.invoke_finish_bundle)

  def teardown(self):
    self._invoke_lifecycle_method(self.do_fn_invoker.invoke_teardown)

  def finalize(self):
    self.bundle_finalizer_param.finalize_bundle()

  def _reraise_augmented(self, exn):
    if getattr(exn, '_tagged_with_step', False) or not self.step_name:
      raise
    step_annotation = " [while running '%s']" % self.step_name
    # To emulate exception chaining (not available in Python 2).
    try:
      # Attempt to construct the same kind of exception
      # with an augmented message.
      new_exn = type(exn)(exn.args[0] + step_annotation, *exn.args[1:])
      new_exn._tagged_with_step = True  # Could raise attribute error.
    except:  # pylint: disable=bare-except
      # If anything goes wrong, construct a RuntimeError whose message
      # records the original exception's type and message.
      new_exn = RuntimeError(
          traceback.format_exception_only(type(exn), exn)[-1].strip()
          + step_annotation)
      new_exn._tagged_with_step = True
    raise_with_traceback(new_exn)


class OutputProcessor(object):

  def process_outputs(self, windowed_input_element, results):
    # type: (WindowedValue, Iterable[Any]) -> None
    raise NotImplementedError


class _OutputProcessor(OutputProcessor):
  """Processes output produced by DoFn method invocations."""

  def __init__(self,
               window_fn,
               main_receivers,  # type: Receiver
               tagged_receivers,  # type: Mapping[Optional[str], Receiver]
               per_element_output_counter):
    """Initializes ``_OutputProcessor``.

    Args:
      window_fn: a windowing function (WindowFn).
      main_receivers: a dict of tag name to Receiver objects.
      tagged_receivers: main receiver object.
      per_element_output_counter: per_element_output_counter of one work_item.
                                  could be none if experimental flag turn off
    """
    self.window_fn = window_fn
    self.main_receivers = main_receivers
    self.tagged_receivers = tagged_receivers
    self.per_element_output_counter = per_element_output_counter

  def process_outputs(self, windowed_input_element, results):
    # type: (WindowedValue, Iterable[Any]) -> None
    """Dispatch the result of process computation to the appropriate receivers.

    A value wrapped in a TaggedOutput object will be unwrapped and
    then dispatched to the appropriate indexed output.
    """
    if results is None:
      # TODO(BEAM-3937): Remove if block after output counter released.
      # Only enable per_element_output_counter when counter cythonized.
      if (self.per_element_output_counter is not None and
          self.per_element_output_counter.is_cythonized):
        self.per_element_output_counter.add_input(0)
      return

    output_element_count = 0
    for result in results:
      # results here may be a generator, which cannot call len on it.
      output_element_count += 1
      tag = None
      if isinstance(result, TaggedOutput):
        tag = result.tag
        if not isinstance(tag, (str, unicode)):
          raise TypeError('In %s, tag %s is not a string' % (self, tag))
        result = result.value
      if isinstance(result, WindowedValue):
        windowed_value = result
        if (windowed_input_element is not None
            and len(windowed_input_element.windows) != 1):
          windowed_value.windows *= len(windowed_input_element.windows)
      elif isinstance(result, TimestampedValue):
        assign_context = WindowFn.AssignContext(result.timestamp, result.value)
        windowed_value = WindowedValue(
            result.value, result.timestamp,
            self.window_fn.assign(assign_context))
        if len(windowed_input_element.windows) != 1:
          windowed_value.windows *= len(windowed_input_element.windows)
      else:
        windowed_value = windowed_input_element.with_value(result)
      if tag is None:
        self.main_receivers.receive(windowed_value)
      else:
        self.tagged_receivers[tag].receive(windowed_value)
    # TODO(BEAM-3937): Remove if block after output counter released.
    # Only enable per_element_output_counter when counter cythonized
    if (self.per_element_output_counter is not None and
        self.per_element_output_counter.is_cythonized):
      self.per_element_output_counter.add_input(output_element_count)

  def start_bundle_outputs(self, results):
    """Validate that start_bundle does not output any elements"""
    if results is None:
      return
    raise RuntimeError(
        'Start Bundle should not output any elements but got %s' % results)

  def finish_bundle_outputs(self, results):
    """Dispatch the result of finish_bundle to the appropriate receivers.

    A value wrapped in a TaggedOutput object will be unwrapped and
    then dispatched to the appropriate indexed output.
    """
    if results is None:
      return

    for result in results:
      tag = None
      if isinstance(result, TaggedOutput):
        tag = result.tag
        if not isinstance(tag, (str, unicode)):
          raise TypeError('In %s, tag %s is not a string' % (self, tag))
        result = result.value

      if isinstance(result, WindowedValue):
        windowed_value = result
      else:
        raise RuntimeError('Finish Bundle should only output WindowedValue ' +\
                           'type but got %s' % type(result))

      if tag is None:
        self.main_receivers.receive(windowed_value)
      else:
        self.tagged_receivers[tag].receive(windowed_value)


class _NoContext(WindowFn.AssignContext):
  """An uninspectable WindowFn.AssignContext."""
  NO_VALUE = object()

  def __init__(self, value, timestamp=NO_VALUE):
    self.value = value
    self._timestamp = timestamp

  @property
  def timestamp(self):
    if self._timestamp is self.NO_VALUE:
      raise ValueError('No timestamp in this context.')
    else:
      return self._timestamp

  @property
  def existing_windows(self):
    raise ValueError('No existing_windows in this context.')


class DoFnState(object):
  """For internal use only; no backwards-compatibility guarantees.

  Keeps track of state that DoFns want, currently, user counters.
  """

  def __init__(self, counter_factory):
    self.step_name = ''
    self._counter_factory = counter_factory

  def counter_for(self, aggregator):
    """Looks up the counter for this aggregator, creating one if necessary."""
    return self._counter_factory.get_aggregator_counter(
        self.step_name, aggregator)


# TODO(robertwb): Replace core.DoFnContext with this.
class DoFnContext(object):
  """For internal use only; no backwards-compatibility guarantees."""

  def __init__(self, label, element=None, state=None):
    self.label = label
    self.state = state
    if element is not None:
      self.set_element(element)

  def set_element(self, windowed_value):
    # type: (Optional[WindowedValue]) -> None
    self.windowed_value = windowed_value

  @property
  def element(self):
    if self.windowed_value is None:
      raise AttributeError('element not accessible in this context')
    else:
      return self.windowed_value.value

  @property
  def timestamp(self):
    if self.windowed_value is None:
      raise AttributeError('timestamp not accessible in this context')
    else:
      return self.windowed_value.timestamp

  @property
  def windows(self):
    if self.windowed_value is None:
      raise AttributeError('windows not accessible in this context')
    else:
      return self.windowed_value.windows
