from __future__ import annotations

import functools
import pickle
import time
import uuid
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from enum import Enum
from threading import Lock
from typing import TYPE_CHECKING, Any

import msgpack

from pydiverse.pipedag.context.context import BaseContext, ConfigContext
from pydiverse.pipedag.errors import LockError, RemoteProcessError, StageError
from pydiverse.pipedag.util.ipc import IPCServer

if TYPE_CHECKING:
    from pydiverse.pipedag import Flow, Stage
    from pydiverse.pipedag._typing import T
    from pydiverse.pipedag.backend import LockState
    from pydiverse.pipedag.core import Task
    from pydiverse.pipedag.materialize import Blob, Table


def synchronized(lock_attr: str):
    def decorator(func: T) -> T:
        @functools.wraps(func)
        def synced_func(self, *args, **kwargs):
            with getattr(self, lock_attr):
                return func(self, *args, **kwargs)

        return synced_func

    return decorator


class RunContextServer(IPCServer):
    """RPC Server that stores all global run state

    FORMAT:
        REQUEST: [method_name, arguments]
        RESPONSE: [error, result]

    Currently, we interrupt socket listening every 200ms for seeing stop() instruction from other thread.
    Alternative: Replace busy waiting with an interrupt based mechanism (eg threading.Event)

    RunContextServer spawns a thread in __enter__(). The actual context is managed in RunContext class. The constructor
    of RunContext() spawns the thread by calling RunContextServer.get_client(). The returned IPCClient object
    supports __getstate__() / __setstate__() methods, so they support serialization and deserialization in case of
    multi-node execution of tasks in pipedag graph.
    """

    def __init__(self, flow: Flow, ignore_fresh_input):
        config_ctx = ConfigContext.get()
        interface = config_ctx.network_interface

        super().__init__(
            listen=f"tcp://{interface}:0",
            msg_default=_msg_default,
            msg_ext_hook=_msg_ext_hook,
        )

        self.flow = flow
        self.ignore_fresh_input = ignore_fresh_input
        self.run_id = uuid.uuid4().hex[:20]

        self.stages = list(self.flow.stages.values())
        self.tasks = list(self.flow.tasks)
        self.stages.sort(key=lambda s: s.id)
        self.tasks.sort(key=lambda t: t.id)

        num_stages = len(self.stages)

        # STATE
        self.ref_count = [0] * num_stages
        self.stage_state = [StageState.UNINITIALIZED] * num_stages

        self.table_names = [set() for _ in range(num_stages)]
        self.blob_names = [set() for _ in range(num_stages)]

        self.task_memo: defaultdict[Any, Any] = defaultdict(lambda: MemoState.NONE)

        # STATE LOCKS
        self.ref_count_lock = Lock()
        self.stage_state_lock = Lock()
        self.names_lock = Lock()
        self.task_memo_lock = Lock()

        # LOCKING
        config_ctx = ConfigContext.get()
        self.lock_manager = config_ctx.lock_manager
        self.lock_manager.add_lock_state_listener(self._lock_state_listener)

    def __enter__(self):
        self.logger.debug("enter context")
        super().__enter__()

        # INITIALIZE EVERYTHING
        with self.lock_manager("_pipedag_setup_"):
            config_ctx = ConfigContext.get()
            config_ctx.store.table_store.setup()

            # Acquire a lock on all stages
            # We must lock all stages from the start to prevent two flows from
            # deadlocking each other (lock order inversion). To lock the stage
            # only when it's needed (may result in deadlocks), the lock should
            # get acquired in the `PipeDAGStore.init_stage` function instead.
            for stage in self.stages:
                self.lock_manager.acquire(stage)

        # INITIALIZE REFERENCE COUNTERS
        for stage in self.stages:
            for task in stage.all_tasks():
                for s in task.upstream_stages:
                    self.ref_count[s.id] += 1

        self.__context_proxy = RunContext(self)
        self.__context_proxy.__enter__()
        self.logger.debug("entered context")
        return self.__context_proxy

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.logger.debug("exit context")
        self._release_all_locks()
        self.__context_proxy.__exit__(exc_type, exc_val, exc_tb)
        super().__exit__(exc_type, exc_val, exc_tb)
        self.lock_manager = None  # don't use lock_manager any more
        self.logger.debug("exited context")

    def handle_request(self, request):
        try:
            op_name, args = request
            op = getattr(self, op_name)
            if not callable(op):
                raise TypeError(f"OP '{op_name}' is not callable")

            result = op(*args)
            return [None, result]
        except Exception as e:
            pickled_exception = pickle.dumps(e)
            return [pickled_exception, None]

    # STAGE: Reference Counting

    @synchronized("ref_count_lock")
    def get_stage_ref_count(self, stage_id: int):
        return self.ref_count[stage_id]

    # STAGE: State

    @synchronized("stage_state_lock")
    def get_stage_state(self, stage_id: int):
        return self.stage_state[stage_id].value

    def enter_init_stage(self, stage_id: int):
        return self._enter_stage_state_transition(
            stage_id,
            StageState.UNINITIALIZED,
            StageState.INITIALIZING,
            StageState.READY,
        )

    def exit_init_stage(self, stage_id: int, success: bool):
        self._exit_stage_state_transition(
            stage_id,
            success,
            StageState.INITIALIZING,
            StageState.READY,
        )

    def enter_commit_stage(self, stage_id: int):
        return self._enter_stage_state_transition(
            stage_id,
            StageState.READY,
            StageState.COMMITTING,
            StageState.COMMITTED,
        )

    def exit_commit_stage(self, stage_id: int, success: bool):
        self._exit_stage_state_transition(
            stage_id,
            success,
            StageState.COMMITTING,
            StageState.COMMITTED,
        )

    def _enter_stage_state_transition(
        self,
        stage_id: int,
        from_: StageState,
        transition: StageState,
        to: StageState,
    ):
        with self.stage_state_lock:
            # Already in correct state
            if self.stage_state[stage_id] == to:
                return False

            # Try to begin transition
            if self.stage_state[stage_id] == from_:
                self.stage_state[stage_id] = transition
                return True

        # Wait until transition is over
        while self.stage_state[stage_id] == transition:
            time.sleep(0.01)

        # Check if transition was successful
        with self.stage_state_lock:
            state = self.stage_state[stage_id]

            if state == to:
                return False
            if state == StageState.FAILED:
                raise StageError(
                    f"Stage '{self.stages[stage_id].name}' is in unexpected state"
                    f" {state}"
                )

    def _exit_stage_state_transition(
        self,
        stage_id: int,
        success: bool,
        transition: StageState,
        to: StageState,
    ):
        if not success:
            with self.stage_state_lock:
                self.stage_state[stage_id] = StageState.FAILED
                return

        with self.stage_state_lock:
            if self.stage_state[stage_id] == transition:
                self.stage_state[stage_id] = to
            else:
                raise RuntimeError

    # LOCKING

    def _lock_state_listener(
        self, stage: Stage, old_state: LockState, new_state: LockState
    ):
        """Internal listener that gets notified when the state of a lock changes"""
        from pydiverse.pipedag.backend import LockState
        from pydiverse.pipedag.core import Stage

        if not isinstance(stage, Stage):
            return

        # Logging
        if new_state == LockState.UNCERTAIN:
            self.logger.warning(
                f"Lock for stage '{stage.name}' transitioned to UNCERTAIN state."
            )
        if old_state == LockState.UNCERTAIN and new_state == LockState.LOCKED:
            self.logger.info(
                f"Lock for stage '{stage.name}' is still LOCKED (after being"
                " UNCERTAIN)."
            )
        if old_state == LockState.UNCERTAIN and new_state == LockState.INVALID:
            self.logger.error(f"Lock for stage '{stage.name}' has become INVALID.")

    def _release_all_locks(self):
        assert (
            self.lock_manager is not None
        ), "this method may only be called between __enter__() and __exit__()"
        locks = list(self.lock_manager.lock_states.items())
        for lock, state in locks:
            self.lock_manager.release(lock)

    def acquire_stage_lock(self, stage_id: int):
        assert (
            self.lock_manager is not None
        ), "this method may only be called between __enter__() and __exit__()"
        stage = self.stages[stage_id]
        self.lock_manager.acquire(stage)

    def release_stage_lock(self, stage_id: int):
        assert (
            self.lock_manager is not None
        ), "this method may only be called between __enter__() and __exit__()"
        stage = self.stages[stage_id]
        self.lock_manager.release(stage)

    def validate_stage_lock(self, stage_id: int):
        assert (
            self.lock_manager is not None
        ), "this method may only be called between __enter__() and __exit__()"
        from pydiverse.pipedag.backend.lock import LockState

        stage = self.stages[stage_id]
        did_log = False
        while True:
            state = self.lock_manager.get_lock_state(stage)

            if state == LockState.LOCKED:
                return
            elif state == LockState.UNLOCKED:
                raise LockError(f"Lock for stage '{stage.name}' is unlocked.")
            elif state == LockState.INVALID:
                raise LockError(f"Lock for stage '{stage.name}' is invalid.")
            elif state == LockState.UNCERTAIN:
                if not did_log:
                    self.logger.info(
                        f"Waiting for stage '{stage.name}' lock state to"
                        " become known again..."
                    )
                    did_log = True

                time.sleep(0.01)

            else:
                raise ValueError(f"Invalid state '{state}'.")

    # TASK

    def did_finish_task(self, task_id: int, final_state_value: int):
        assert (
            self.lock_manager is not None
        ), "this method may only be called between __enter__() and __exit__()"

        # TODO: Do something with the final state.
        #       For example: The object returned by flow.run could have a list
        #       of tasks and their final states.
        final_state = FinalTaskState(final_state_value)
        task = self.tasks[task_id]

        stages_to_release = []

        with self.ref_count_lock:
            for stage in task.upstream_stages:
                self.ref_count[stage.id] -= 1
                rc = self.ref_count[stage.id]

                if rc == 0:
                    stages_to_release.append(stage)
                elif rc < 0:
                    self.logger.error(
                        "Reference counter is negative.",
                        reference_count=rc,
                        stage=stage,
                    )

        for stage in stages_to_release:
            self.lock_manager.release(stage)

    def get_memo_key(self, task, cache_keys: dict[str, str]):
        sub_key = "-".join(cache_keys.values())
        memo_key = (task.stage.id, sub_key)
        return memo_key

    def enter_task_memo(
        self, task_id: int, cache_keys: dict[str, str]
    ) -> tuple[bool, Any]:
        task = self.tasks[task_id]

        with self.task_memo_lock:
            memo_key = self.get_memo_key(task, cache_keys)
            memo = self.task_memo[memo_key]
            if memo is MemoState.NONE:
                self.task_memo[memo_key] = MemoState.WAITING
                return False, None

        if memo is MemoState.WAITING:
            self.logger.info(
                "Task is currently being run with the same inputs."
                " Waiting for the other task to finish...",
                task=task,
            )
        while memo is MemoState.WAITING:
            time.sleep(0.01)
            memo = self.task_memo[memo_key]

        if memo is MemoState.FAILED:
            # Should this get handled differently?
            raise Exception("The same task already failed earlier (memo).")
        if isinstance(memo, MemoState):
            raise Exception

        return True, memo

    @synchronized("task_memo_lock")
    def exit_task_memo(self, task_id: int, cache_keys: dict[str, str], success: bool):
        task = self.tasks[task_id]
        memo_key = self.get_memo_key(task, cache_keys)

        if not success:
            self.task_memo[memo_key] = MemoState.FAILED
            return

        memo = self.task_memo[memo_key]
        if isinstance(memo, MemoState):
            raise Exception("set_task_memo not called")

    @synchronized("task_memo_lock")
    def store_task_memo(self, task_id: int, cache_keys: dict[str, str], value: Any):
        task = self.tasks[task_id]
        memo_key = self.get_memo_key(task, cache_keys)
        memo = self.task_memo[memo_key]

        if memo is not MemoState.WAITING:
            raise Exception("Expected memo state to be waiting")

        self.task_memo[memo_key] = value

    # TABLE / BLOB: Names

    @synchronized("names_lock")
    def add_names(self, stage_id: int, tables: list[str], blobs: list[str]):
        table_duplicates = []
        blob_duplicates = []

        for table in tables:
            if table in self.table_names[stage_id]:
                table_duplicates.append(table)
        for blob in blobs:
            if blob in self.blob_names[stage_id]:
                blob_duplicates.append(blob)

        if table_duplicates or blob_duplicates:
            return {
                "success": False,
                "table_duplicates": table_duplicates,
                "blob_duplicates": blob_duplicates,
            }

        for table in tables:
            self.table_names[stage_id].add(table)
        for blob in blobs:
            self.blob_names[stage_id].add(blob)

        return {
            "success": True,
        }

    @synchronized("names_lock")
    def remove_names(self, stage_id: int, tables: list[str], blobs: list[str]):
        for table in tables:
            self.table_names[stage_id].remove(table)
        for blob in blobs:
            self.blob_names[stage_id].remove(blob)


class RunContext(BaseContext):
    _context_var = ContextVar("run_context_proxy")

    def __init__(self, server: RunContextServer):
        self.client = server.get_client()
        self.flow = server.flow
        self.ignore_fresh_input = server.ignore_fresh_input
        self.run_id = server.run_id

    def get_cache_key_type(self):
        return "tasks_only" if self.ignore_fresh_input else "default"

    @staticmethod
    def get_cache_key_types():
        return ["default", "tasks_only"]

    def _request(self, op: str, *args):
        error, result = self.client.request([op, args])

        if error is not None:
            raise RemoteProcessError("Request failed") from pickle.loads(error)
        return result

    # STAGE: Reference Counting

    def get_stage_ref_count(self, stage: Stage) -> int:
        return self._request("get_stage_ref_count", stage.id)

    # STAGE: State

    def get_stage_state(self, stage: Stage) -> StageState:
        value = self._request("get_stage_state", stage.id)
        return StageState(value)

    @contextmanager
    def init_stage(self, stage: Stage):
        can_continue = self._request("enter_init_stage", stage.id)
        if not can_continue:
            yield False
            return

        try:
            yield True
        except Exception as e:
            # Failed initializing stage
            self._request("exit_init_stage", stage.id, False)
            raise StageError from e
        else:
            self._request("exit_init_stage", stage.id, True)

    @contextmanager
    def commit_stage(self, stage: Stage):
        can_continue = self._request("enter_commit_stage", stage.id)
        if not can_continue:
            yield False
            return

        try:
            yield True
        except Exception as e:
            # Failed committing stage
            self._request("exit_commit_stage", stage.id, False)
            raise StageError from e
        else:
            self._request("exit_commit_stage", stage.id, True)

    # STAGE: Lock

    def acquire_stage_lock(self, stage: Stage):
        self._request("acquire_stage_lock", stage.id)

    def release_stage_lock(self, stage: Stage):
        self._request("release_stage_lock", stage.id)

    def validate_stage_lock(self, stage: Stage):
        self._request("validate_stage_lock", stage.id)

    # TASK

    def did_finish_task(self, task: Task, final_state: FinalTaskState):
        self._request("did_finish_task", task.id, final_state.value)

    @contextmanager
    def task_memo(self, task: Task, cache_keys: dict[str, str]):
        success, memo = self._request("enter_task_memo", task.id, cache_keys)

        try:
            yield success, memo
        except Exception as e:
            self._request("exit_task_memo", task.id, cache_keys, False)
            raise e
        else:
            self._request("exit_task_memo", task.id, cache_keys, True)

    def store_task_memo(self, task: Task, cache_keys: dict[str, str], result: Any):
        self._request("store_task_memo", task.id, cache_keys, result)

    # TABLE / BLOB: Names

    def add_names(
        self, stage: Stage, tables: list[Table], blobs: list[Blob]
    ) -> tuple[bool, tuple[str, ...], tuple[str, ...]]:
        response = self._request(
            "add_names",
            stage.id,
            [t.name for t in tables],
            [b.name for b in blobs],
        )

        if response["success"]:
            return True, (), ()
        else:
            return False, response["table_duplicates"], response["blob_duplicates"]

    def remove_names(self, stage: Stage, tables: list[Table], blobs: list[Blob]):
        self._request(
            "remove_names",
            stage.id,
            [t.name for t in tables],
            [b.name for b in blobs],
        )


class DematerializeRunContext(BaseContext):
    """Dummy RunContext that returns `COMMITTED` as the stage state for all stages

    To dematerialize an object we must know it the associates state has been committed or not.
    This context replaces the RunContext created by RunContextServer during the execution of
    a flow with one that only sets the state of each state to `COMMITTED`. This allows us
    to dematerialize the objects that have been stored after a flow has finished running.
    """

    _context_var = RunContext._context_var

    def get_stage_state(self, stage: Stage) -> StageState:
        return StageState.COMMITTED

    def validate_stage_lock(self, stage: Stage):
        pass


# States


class StageState(Enum):
    UNINITIALIZED = 0
    INITIALIZING = 1
    READY = 2
    COMMITTING = 3
    COMMITTED = 4

    FAILED = 127


class FinalTaskState(Enum):
    UNKNOWN = 0

    COMPLETED = 1
    FAILED = 2
    SKIPPED = 3


class MemoState(Enum):
    NONE = 0
    WAITING = 1

    FAILED = 127


# msgpack hooks
# Only used to transmit Table and Blob type metadata


def _msg_default(obj):
    return msgpack.ExtType(0, pickle.dumps(obj))


def _msg_ext_hook(code, data):
    if code == 0:
        return pickle.loads(data)
    return msgpack.ExtType(code, data)
