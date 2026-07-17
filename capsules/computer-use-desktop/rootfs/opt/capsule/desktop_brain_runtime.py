#!/usr/bin/env python3
"""Deterministic, durable, single-worker desktop task execution kernel."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping

from control_client import ControlClient, ControlError, FreezeBarrier, HumanPreempted
from evidence import EvidenceGate, EvidenceStore, MissingEvidence, redact
from policy import ApprovalAuthority, PolicyEngine
from presentation import PresentationSink
from skills import SkillContext, SkillRegistry, default_registry
from skills.base import RawResult, Verification
from state_fusion import DesktopSnapshot, StateFusion
from task_contract import (
    ActionEnvelope,
    IdleResumePolicy,
    Interruptibility,
    PresentationMode,
    Step,
    TaskContract,
    TaskContractRevision,
    TaskState,
    TERMINAL_STATES,
    apply_revision,
    evaluate_evidence_assertions,
)
from task_journal import ApprovalConflict, IdempotencyConflict, TaskJournal
from task_memory import TaskMemory


DEFAULT_STATE_PATH = os.environ.get("PAIRPUTER_DESKTOP_BRAIN_DB", "/home/app/.local/state/pairputer/brain.sqlite3")


class BrainRuntime:
    """One background worker, one effect lease, and no implicit authority."""

    def __init__(
        self,
        database_path: str | Path = DEFAULT_STATE_PATH,
        *,
        state_fusion: StateFusion | None = None,
        control: ControlClient | None = None,
        registry: SkillRegistry | None = None,
        services: Mapping[str, Any] | None = None,
        approval_secret: bytes | None = None,
    ):
        self.database_path = str(database_path)
        self.journal = TaskJournal(self.database_path)
        self.memory = TaskMemory(self.database_path)
        self.evidence = EvidenceStore(self.database_path)
        self.evidence_gate = EvidenceGate(self.evidence)
        self.state_fusion = state_fusion or StateFusion()
        self.control = control or ControlClient(lambda: self.state_fusion.world_revision)
        self.state_fusion.set_human_epoch_provider(lambda: self.control.human_epoch)
        self.policy = PolicyEngine()
        self.approvals = ApprovalAuthority(self.journal, approval_secret)
        self.registry = registry or default_registry()
        self.presentation = PresentationSink()
        self.services = dict(services or {})
        self._contracts: dict[str, TaskContract] = {}
        self._plans: dict[str, tuple[Step, ...]] = {}
        self._action_approval_tokens: dict[str, str] = {}
        self._pending_approvals: dict[str, dict[str, Any]] = {}
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._worker_guard = asyncio.Lock()
        self._active_task_id: str | None = None
        self._quiescent = asyncio.Event()
        self._quiescent.set()
        self._accepting_effects = True
        self._closed = False
        self.control.subscribe_preemption(self._on_human_preemption)

    @property
    def active_task_id(self) -> str | None:
        return self._active_task_id

    @property
    def worker_count(self) -> int:
        return int(self._worker is not None and not self._worker.done())

    async def start(self) -> None:
        await self.recover()
        await self._ensure_worker()

    async def close(self) -> None:
        self._closed = True
        if self._worker and not self._worker.done():
            await self._queue.put(None)
            await self._worker
        self.journal.flush()
        self.evidence.close()
        self.memory.close()
        self.journal.close()

    async def submit_task(
        self,
        request: Mapping[str, Any],
        *,
        plan: tuple[Step, ...] | list[Mapping[str, Any]] | None = None,
        conflict: str = "reject",
        created_by: str = "direct_human",
    ) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("brain runtime is closed")
        if not self._accepting_effects:
            raise FreezeBarrier("new tasks are blocked by a freeze barrier")
        existing = self._effect_capable_task()
        if existing:
            if conflict == "reject":
                raise ActiveTaskConflict(existing)
            if conflict == "replace":
                await self.cancel_task(existing, reason="explicitly replaced")
            elif conflict != "queue":
                raise ValueError("conflict must be reject, replace, or queue")
        contract = TaskContract.compile(request, human_epoch=self.control.human_epoch, created_by=created_by)
        # Validate and normalize host-provided plans before mutating any durable state.  A malformed
        # plan must fail as a rejected submission, never leave an orphan QUEUED task that blocks the
        # next request under the default conflict=reject policy.
        steps = self._normalize_plan(plan)
        if steps:
            self._validate_plan(contract, steps)
        self._contracts[contract.task_id] = contract
        await self._register_task_grant(contract)
        try:
            self.journal.create_task(contract.task_id, contract.as_dict(), contract.digest)
        except BaseException:
            await self._revoke_task_grant(contract.task_id)
            self._contracts.pop(contract.task_id, None)
            raise
        for constraint in contract.constraints:
            self.memory.record_constraint(contract.task_id, constraint, revision=0, source=created_by)
        if steps:
            self._plans[contract.task_id] = steps
            self.journal.append_event(contract.task_id, "PLAN_COMPILED", {"steps": [step.as_dict() for step in steps]})
        await self._ensure_worker()
        await self._queue.put(contract.task_id)
        return self.status(contract.task_id)

    async def continue_task(
        self,
        task_id: str,
        *,
        plan: tuple[Step, ...] | list[Mapping[str, Any]] | None = None,
        revision: TaskContractRevision | None = None,
        action_approval_token: str | None = None,
        trigger: str = "explicit",
        idle_seconds: float = 0,
        visible: bool = True,
        capsule_running: bool = True,
    ) -> dict[str, Any]:
        if not self._accepting_effects:
            raise FreezeBarrier("cannot continue while frozen")
        state = self.journal.state(task_id)
        if state in TERMINAL_STATES:
            return self.status(task_id)
        if state in {TaskState.QUEUED, TaskState.COMPILING, TaskState.READY, TaskState.RUNNING}:
            raise TaskAlreadyRunning(f"task {task_id} is already executing or queued")
        contract = self._contract(task_id)
        if trigger == "idle":
            self._assert_idle_eligible(contract, state, idle_seconds=idle_seconds, visible=visible, capsule_running=capsule_running)
        elif trigger != "explicit":
            raise ValueError("resume trigger must be explicit or idle")
        candidate_contract = apply_revision(contract, revision) if revision is not None else contract
        candidate_steps = self._normalize_plan(plan) if plan is not None else self._plans.get(task_id) or self._load_plan(task_id)
        if candidate_steps:
            self._validate_plan(candidate_contract, candidate_steps)
        if revision is not None:
            contract = candidate_contract
            self._contracts[task_id] = contract
            self.journal.replace_contract(task_id, contract.as_dict(), contract.digest, revision.as_dict())
            for values in revision.additions.values():
                for value in values:
                    self.memory.record_constraint(task_id, str(value), revision=contract.revision, source=revision.created_by)
        if plan is not None:
            if state == TaskState.WAITING_FOR_APPROVAL:
                self.journal.expire_approvals(task_id, reason="replacement plan invalidated approval")
            if not candidate_steps:
                raise ValueError("continued plan must contain at least one step")
            self._plans[task_id] = candidate_steps
            self.journal.append_event(task_id, "PLAN_REVISED", {"steps": [step.as_dict() for step in candidate_steps]})
        if action_approval_token:
            self._action_approval_tokens[task_id] = action_approval_token
        elif state == TaskState.WAITING_FOR_APPROVAL and plan is None:
            raise ApprovalConflict("waiting approval requires the exact granted token or a replacement plan")
        self.journal.set_meta(f"resume_trigger:{task_id}", trigger)
        await self._register_task_grant(contract)
        if state in {
            TaskState.PAUSED_BY_HUMAN, TaskState.WAITING_FOR_HOST,
            TaskState.WAITING_FOR_USER, TaskState.WAITING_FOR_APPROVAL,
        }:
            self.journal.transition(task_id, TaskState.RECONCILING, reason=f"{trigger} resume requested")
        await self._ensure_worker()
        await self._queue.put(task_id)
        return self.status(task_id)

    def approve(self, approval_id: str) -> str:
        """Explicit human approval action; the returned token is still not execution."""

        return self.approvals.approve(approval_id)

    async def cancel_task(self, task_id: str, *, reason: str = "explicit cancel") -> dict[str, Any]:
        state = self.journal.state(task_id)
        if state not in TERMINAL_STATES:
            self.journal.transition(task_id, TaskState.CANCELED, reason=reason)
            self.journal.expire_approvals(task_id, reason="task canceled")
            if self._active_task_id == task_id:
                self.control.release()
        await self._revoke_task_grant(task_id)
        return self.status(task_id)

    def status(self, task_id: str) -> dict[str, Any]:
        # An unknown/pruned task_id must return a clean not-found, not a KeyError that
        # the bridge surfaces as an opaque HTTP 500 — a model polling a stale id (e.g.
        # after a freeze dropped the task) can't interpret a 500.
        try:
            task = self.journal.task(task_id)
        except KeyError:
            return {"accepted": False, "found": False, "task_id": task_id,
                    "state": "UNKNOWN", "reason": "no such task",
                    "human_epoch": self.control.human_epoch,
                    "world_revision": self.state_fusion.world_revision}
        evidence = self.evidence.list_for_task(task_id)
        events = self.journal.events(task_id, after=max(0, self._last_sequence(task_id) - 25), limit=25)
        pending = next((value for value in self._pending_approvals.values() if value["task_id"] == task_id), None)
        return {
            "accepted": True,
            "task_id": task_id,
            "state": task["state"],
            "current_step": task["current_step"],
            "progress": {
                "completed_steps": task["current_step"],
                "total_steps": len(self._plans.get(task_id, ())),
            },
            "needs": task["needs"] or ({"type": "approval", **pending} if pending else None),
            "evidence": [asdict(item) for item in evidence],
            "warnings": [],
            "human_epoch": self.control.human_epoch,
            "world_revision": self.state_fusion.world_revision,
            "events": events,
        }

    async def freeze_barrier(self) -> None:
        self._accepting_effects = False
        self.control.begin_freeze()
        try:
            await asyncio.wait_for(self._quiescent.wait(), timeout=1.0)
            quiescent = True
        except asyncio.TimeoutError:
            quiescent = False
        task_id = self._active_task_id or self._effect_capable_task()
        if task_id:
            state = self.journal.state(task_id)
            if state == TaskState.RUNNING:
                self.journal.transition(task_id, TaskState.PAUSED_BY_HUMAN, reason="freeze barrier")
            self.journal.append_event(task_id, "FREEZE_BARRIER", {
                "human_epoch": self.control.human_epoch,
                "world_revision": self.state_fusion.world_revision,
                "quiescent": quiescent,
            })
            if not quiescent:
                self.journal.append_event(task_id, "FREEZE_INFLIGHT_UNKNOWN", {
                    "classification": "inspect_after_thaw_before_any_retry",
                })
        self.journal.expire_approvals(reason="freeze barrier")
        self.journal.set_meta("freeze_barrier", {
            "active": True, "human_epoch": self.control.human_epoch,
            "world_revision": self.state_fusion.world_revision, "at": time.time(),
        })
        self.journal.flush()

    async def thaw_reconcile(self) -> None:
        self.control.end_thaw()
        self.state_fusion.advance_revision()
        self._accepting_effects = True
        self.journal.expire_approvals(reason="thaw invalidated approvals")
        self.journal.set_meta("freeze_barrier", {"active": False, "at": time.time()})
        for task in self.journal.active_tasks():
            task_id, state = task["task_id"], TaskState(task["state"])
            if state == TaskState.PAUSED_BY_HUMAN:
                self.journal.transition(task_id, TaskState.RECONCILING, reason="thawed; explicit or eligible idle resume still required")
            self.journal.append_event(task_id, "THAW_REQUIRES_RECONCILIATION", {
                "human_epoch": self.control.human_epoch,
                "world_revision": self.state_fusion.world_revision,
            })
        # Deliberately no queue submission: thaw never grants execution authority.

    async def recover(self) -> None:
        """Fail safe after restart: human owns control and effects do not auto-resume."""

        self.control.reset_to_human()
        await self._clear_task_grants()
        self.journal.expire_approvals(reason="runtime restart")
        for row in self.journal.active_tasks():
            task_id, state = row["task_id"], TaskState(row["state"])
            self._contract(task_id)
            self._load_plan(task_id)
            if state in {TaskState.RUNNING, TaskState.READY, TaskState.WAITING_FOR_APPROVAL}:
                self.journal.transition(task_id, TaskState.RECONCILING, reason="runtime recovery requires explicit resume")
            elif state == TaskState.COMPILING:
                self.journal.transition(task_id, TaskState.WAITING_FOR_HOST, reason="compile interrupted by runtime restart")
            self.journal.append_event(task_id, "RUNTIME_RECOVERED", {
                "previous_state": state.value, "control_owner": "HUMAN",
            })

    async def _ensure_worker(self) -> None:
        async with self._worker_guard:
            if self._worker is None or self._worker.done():
                self._worker = asyncio.create_task(self._worker_loop(), name="desktop-brain-worker")

    async def _worker_loop(self) -> None:
        while not self._closed:
            task_id = await self._queue.get()
            if task_id is None:
                self._queue.task_done()
                break
            try:
                state = self.journal.state(task_id)
                if state not in TERMINAL_STATES:
                    self._active_task_id = task_id
                    await self._drive(task_id)
            except Exception as exc:
                state = self.journal.state(task_id)
                if state not in TERMINAL_STATES and state not in {
                    TaskState.PAUSED_BY_HUMAN, TaskState.WAITING_FOR_HOST,
                    TaskState.WAITING_FOR_USER, TaskState.WAITING_FOR_APPROVAL,
                }:
                    self.journal.transition(task_id, TaskState.FAILED, reason=type(exc).__name__)
                self.journal.append_event(task_id, "TASK_DRIVER_ERROR", {"error": type(exc).__name__})
            finally:
                try:
                    if self.journal.state(task_id) in TERMINAL_STATES:
                        await self._revoke_task_grant(task_id)
                except Exception as exc:
                    self.journal.append_event(task_id, "TASK_GRANT_REVOKE_FAILED", {
                        "error": type(exc).__name__,
                    })
                if self._active_task_id == task_id:
                    self._active_task_id = None
                self.control.release()
                self._queue.task_done()

    async def _drive(self, task_id: str) -> None:
        contract = self._contract(task_id)
        state = self.journal.state(task_id)
        if state == TaskState.QUEUED:
            self.journal.transition(task_id, TaskState.COMPILING)
            plan = self._plans.get(task_id) or self._load_plan(task_id)
            if not plan:
                # Reassert the durable task's exact browser scope at the state
                # boundary. This is idempotent and closes a startup/recovery
                # race where desktopd may have cleared its in-memory grants
                # after submission but before the host-reasoning wait became
                # observable.
                await self._register_task_grant(contract)
                self.journal.transition(task_id, TaskState.WAITING_FOR_HOST, needs=self._needs_host(contract))
                return
            self.journal.transition(task_id, TaskState.READY)
            state = TaskState.READY
        plan = self._plans.get(task_id) or self._load_plan(task_id)
        if not plan:
            if state == TaskState.RECONCILING:
                await self._register_task_grant(contract)
                self.journal.transition(task_id, TaskState.WAITING_FOR_HOST, needs=self._needs_host(contract))
            return
        previous = self.state_fusion.last_snapshot
        if state == TaskState.RECONCILING:
            snapshot = await self.state_fusion.observe()
            reconciliation = self.state_fusion.reconcile(previous, snapshot)
            self.journal.append_event(task_id, "WORLD_RECONCILED", asdict(reconciliation))
            if not reconciliation.in_scope:
                self.journal.transition(task_id, TaskState.WAITING_FOR_USER, needs={"type": "reconciliation", "summary": reconciliation.summary})
                return
            self.journal.transition(task_id, TaskState.RUNNING)
        elif state == TaskState.READY:
            self.journal.transition(task_id, TaskState.RUNNING)
        elif state != TaskState.RUNNING:
            return

        task_row = self.journal.task(task_id)
        index = int(task_row["current_step"])
        burst_limit = self._resume_burst_limit(contract, task_id)
        burst_count = 0
        while index < len(plan) and index < contract.max_steps and burst_count < burst_limit:
            if self.journal.state(task_id) != TaskState.RUNNING:
                return
            step = plan[index]
            if contract.deadline and time.time() >= contract.deadline:
                self.journal.transition(task_id, TaskState.FAILED, reason="task deadline expired")
                return
            await self._execute_step(contract, step)
            if self.journal.state(task_id) != TaskState.RUNNING:
                return
            index += 1
            burst_count += 1
            self.journal.update_step(task_id, index)
        if index < len(plan):
            self.journal.transition(task_id, TaskState.WAITING_FOR_USER, needs={"type": "resume_burst_complete", "next_step": index})
            return
        if not await self._refresh_assertion_evidence(contract, plan):
            return
        final_snapshot = await self.state_fusion.observe()
        final_lease = None
        try:
            # Success is itself an authority-sensitive state transition. Bind
            # it to the same human epoch and world revision as the final proof;
            # a takeover or semantic change between verification and commit
            # invalidates the lease instead of producing stale terminal success.
            final_lease = self.control.acquire(
                task_id=task_id, action_id=f"finalize:{task_id}",
                expected_human_epoch=final_snapshot.human_epoch,
                expected_world_revision=final_snapshot.world_revision,
            )
            self.evidence_gate.assert_complete(
                task_id, contract.success_predicates,
                minimum_world_revision=final_snapshot.world_revision,
                required_references=self._plan_assertion_references(plan),
            )
        except MissingEvidence as exc:
            self.journal.transition(task_id, TaskState.WAITING_FOR_USER, needs={"type": "missing_evidence", "predicates": exc.predicates})
            return
        except HumanPreempted:
            if self.journal.state(task_id) == TaskState.RUNNING:
                self.journal.transition(task_id, TaskState.PAUSED_BY_HUMAN,
                                        reason="human input preempted final verification")
            return
        except ControlError:
            if self.journal.state(task_id) == TaskState.RUNNING:
                self.journal.transition(task_id, TaskState.RECONCILING,
                                        reason="world changed before final verification committed")
            return
        try:
            self.control.atomic_commit(final_lease, lambda: self.journal.transition(
                task_id, TaskState.SUCCEEDED,
                reason="all declared success predicates have verified evidence",
            ))
        except HumanPreempted:
            if self.journal.state(task_id) == TaskState.RUNNING:
                self.journal.transition(task_id, TaskState.PAUSED_BY_HUMAN,
                                        reason="human input preempted final success")
            return
        except ControlError:
            if self.journal.state(task_id) == TaskState.RUNNING:
                self.journal.transition(task_id, TaskState.RECONCILING,
                                        reason="world changed before final success")
            return
        finally:
            self.control.release(final_lease)
        self.journal.append_event(task_id, "TASK_SUCCEEDED", {"evidence_predicates": contract.success_predicates})

    async def _execute_step(self, contract: TaskContract, step: Step) -> None:
        task_id = contract.task_id
        assumptions = self.memory.blocking_assumptions(task_id)
        if assumptions:
            self.journal.transition(task_id, TaskState.WAITING_FOR_USER, needs={"type": "blocking_assumption", "assumptions": assumptions})
            return
        snapshot = await self.state_fusion.observe()
        if snapshot.human_epoch != self.control.human_epoch:
            raise HumanPreempted("snapshot and control epoch diverged")
        skill = self.registry.get(step.skill)
        action = dict(skill.canonical_action(step.arguments))
        decision = self.policy.evaluate(contract, action)
        self.journal.append_event(task_id, "POLICY_EVALUATED", {"step_id": step.step_id, **asdict(decision)})
        if not decision.allowed or decision.requires_human_takeover:
            self.journal.transition(task_id, TaskState.WAITING_FOR_USER, needs={
                "type": "policy", "reason": decision.reason,
                "human_takeover": decision.requires_human_takeover,
            })
            return
        step_binding = json.dumps(step.as_dict(), sort_keys=True, separators=(",", ":"), default=str)
        action_id = "action_" + hashlib.sha256(
            f"{task_id}:{step.step_id}:{json.dumps(action, sort_keys=True, default=str)}:{step_binding}".encode()
        ).hexdigest()[:24]
        idempotency_key = hashlib.sha256(f"{task_id}:{step.step_id}:{action_id}".encode()).hexdigest()
        envelope = ActionEnvelope(
            task_id=task_id, step_id=step.step_id, action_id=action_id,
            expected_world_revision=snapshot.world_revision,
            expected_human_epoch=snapshot.human_epoch,
            idempotency_key=idempotency_key,
            effect_class=decision.effect_class.value, risk_class=decision.risk_class.value,
            interruptibility=step.interruptibility,
            presentation_mode=step.presentation_mode,
            deadline=contract.deadline,
            action=action,
        )
        if decision.requires_approval:
            token = self._action_approval_tokens.pop(task_id, None)
            if not token:
                request = self.approvals.request(envelope, self._approval_preview(envelope, decision))
                pending = {
                    "approval_id": request.approval_id, "task_id": task_id,
                    "step_id": step.step_id, "action_id": action_id,
                    "expires_at": request.expires_at, "preview": request.preview,
                }
                self._pending_approvals[request.approval_id] = pending
                self.journal.transition(task_id, TaskState.WAITING_FOR_APPROVAL, needs={"type": "approval", **pending})
                return
            try:
                self.approvals.validate_and_consume(token, envelope)
            except ApprovalConflict:
                self.journal.expire_approvals(task_id, reason="approval binding changed")
                request = self.approvals.request(envelope, self._approval_preview(envelope, decision))
                pending = {
                    "approval_id": request.approval_id, "task_id": task_id,
                    "step_id": step.step_id, "action_id": action_id,
                    "expires_at": request.expires_at, "preview": request.preview,
                }
                self._pending_approvals[request.approval_id] = pending
                self.journal.transition(task_id, TaskState.WAITING_FOR_APPROVAL, needs={"type": "approval", **pending, "reason": "previous approval became stale"})
                return
            for approval_id, pending in tuple(self._pending_approvals.items()):
                if pending["task_id"] == task_id:
                    self._pending_approvals.pop(approval_id, None)

        context = SkillContext(
            task_id=task_id, step_id=step.step_id, action_id=action_id,
            snapshot=snapshot, control=self.control,
            services={
                **self.services,
                "allowed_capabilities": contract.allowed_capabilities,
                "allowed_domains": contract.allowed_domains,
                "allowed_apps": contract.allowed_apps,
            },
        )
        preconditions = await skill.inspect(step.arguments, snapshot, context)
        if preconditions.get("ready") is False:
            self.journal.transition(task_id, TaskState.WAITING_FOR_USER, needs={"type": "precondition", "detail": preconditions})
            return
        prepared = await skill.prepare(step.arguments, snapshot, context)
        status = self.journal.begin_action(task_id, idempotency_key, envelope.digest)
        if status == "VERIFIED":
            return
        if status in {"COMMITTED", "UNKNOWN_OUTCOME", "PREPARED"}:
            # A restart/timeout never proves non-commit. Verify durable state; never replay.
            if status == "PREPARED":
                self.journal.mark_action(task_id, idempotency_key, "UNKNOWN_OUTCOME", result={"reason": "interrupted after preparation"})
            prior = self.journal.idempotency(task_id, idempotency_key) or {}
            recovery_raw = RawResult(
                committed=status == "COMMITTED",
                result=prior.get("result") or {},
                retry_safety="unknown_outcome",
            )
            try:
                recovery_snapshot = await self.state_fusion.observe()
                verification = await skill.verify(prepared, recovery_raw, recovery_snapshot, context)
                verification = self._verify_evidence_assertions(step, verification)
                if verification.verified and all(verification.predicates.values()):
                    evidence_ids = self._record_verification_evidence(
                        task_id, step, decision.effect_class.value, verification, recovery_snapshot,
                    )
                    if not self.evidence.missing(task_id, step.success_predicates):
                        self.journal.mark_action(
                            task_id, idempotency_key, "VERIFIED",
                            result={"recovered_verification": asdict(verification)}, evidence_ids=evidence_ids,
                        )
                        return
            except Exception as exc:
                self.journal.append_event(task_id, "RECOVERY_VERIFICATION_FAILED", {
                    "idempotency_key": idempotency_key, "error": type(exc).__name__,
                })
            self.journal.transition(task_id, TaskState.WAITING_FOR_USER, needs={
                "type": "unknown_outcome", "idempotency_key": idempotency_key,
                "retry": "inspect before deciding",
            })
            return
        lease = self.control.acquire(
            task_id=task_id, action_id=action_id,
            expected_human_epoch=envelope.expected_human_epoch,
            expected_world_revision=envelope.expected_world_revision,
        )
        self.presentation.emit(
            actor="agent", event_type="action_started", intent=step.skill,
            method=self._presentation_method(decision.effect_class.value), status="executing",
            task_id=task_id, action_id=action_id,
        )
        self._quiescent.clear()
        try:
            raw = await skill.execute(prepared, lease, context)
            self.journal.mark_action(task_id, idempotency_key, "COMMITTED", result=raw.result)
        except HumanPreempted:
            self.journal.mark_action(task_id, idempotency_key, "UNKNOWN_OUTCOME", result={"reason": "human preempted"})
            if self.journal.state(task_id) == TaskState.RUNNING:
                self.journal.transition(task_id, TaskState.PAUSED_BY_HUMAN, reason="human preempted effect")
            return
        except ControlError as exc:
            self.journal.mark_action(task_id, idempotency_key, "UNKNOWN_OUTCOME", result={"reason": type(exc).__name__})
            if self.journal.state(task_id) == TaskState.RUNNING:
                self.journal.transition(task_id, TaskState.PAUSED_BY_HUMAN, reason=type(exc).__name__)
            return
        except Exception as exc:
            self.journal.mark_action(task_id, idempotency_key, "FAILED", result={"error": type(exc).__name__})
            recovery = await skill.recover(exc, snapshot, context)
            self.journal.append_event(task_id, "RECOVERY_PROPOSED", asdict(recovery))
            if recovery.disposition == "ask":
                self.journal.transition(task_id, TaskState.WAITING_FOR_USER, needs={"type": "recovery", "reason": recovery.reason})
            else:
                self.journal.transition(task_id, TaskState.FAILED, reason=type(exc).__name__)
            return
        finally:
            self.control.release(lease)
            self._quiescent.set()

        new_snapshot = await self.state_fusion.observe()
        verification = await skill.verify(prepared, raw, new_snapshot, context)
        verification = self._verify_evidence_assertions(step, verification)
        evidence_ids = self._record_verification_evidence(
            task_id, step, decision.effect_class.value, verification, new_snapshot,
        )
        if not verification.verified or not all(verification.predicates.values()):
            self.journal.mark_action(task_id, idempotency_key, "FAILED", result={"verification": asdict(verification)}, evidence_ids=evidence_ids)
            self.journal.transition(task_id, TaskState.FAILED, reason="declared postcondition was not observed")
            return
        missing_step = self.evidence.missing(
            task_id, step.success_predicates,
            minimum_world_revision=new_snapshot.world_revision,
            required_references=self._step_assertion_references(step),
        )
        if missing_step:
            self.journal.mark_action(task_id, idempotency_key, "FAILED", result={"missing_step_evidence": missing_step}, evidence_ids=evidence_ids)
            self.journal.transition(task_id, TaskState.FAILED, reason="skill did not prove every step success predicate")
            return
        self.journal.mark_action(task_id, idempotency_key, "VERIFIED", result={"verification": asdict(verification)}, evidence_ids=evidence_ids)
        self.presentation.emit(
            actor="agent", event_type="action_verified", intent=step.skill,
            method="verification", status="verified", task_id=task_id,
            action_id=action_id, detail={"evidence_ids": evidence_ids},
        )

    def _on_human_preemption(self, event: Any) -> None:
        self.state_fusion.advance_revision()
        task_id = self._active_task_id or self._effect_capable_task()
        if not task_id:
            return
        self.journal.expire_approvals(task_id, reason="human epoch advanced")
        self.journal.append_event(task_id, "HUMAN_PREEMPTED", asdict(event))
        state = self.journal.state(task_id)
        if state in {TaskState.RUNNING, TaskState.WAITING_FOR_APPROVAL}:
            self.journal.transition(task_id, TaskState.PAUSED_BY_HUMAN, reason="authenticated human input")
        self.presentation.emit(
            actor="human", event_type="human_preempted", intent="human input",
            method="control", status="agent paused", task_id=task_id,
            detail={"human_epoch": event.human_epoch},
        )

    def _record_verification_evidence(
        self, task_id: str, step: Step, kind: str, verification: Any,
        snapshot: DesktopSnapshot,
    ) -> list[str]:
        evidence_ids: list[str] = []
        assertion_results = evaluate_evidence_assertions(
            step.evidence_assertions, verification.observed,
        )
        for predicate, verified in verification.predicates.items():
            assertion = assertion_results.get(predicate)
            item = self.evidence.record(
                task_id=task_id, step_id=step.step_id, predicate=predicate,
                kind=f"typed_assertion:{kind}" if assertion else kind,
                reference=(f"assertion:{assertion['spec_digest']}" if assertion else "inline:metadata"),
                observed=verification.observed,
                world_revision=snapshot.world_revision, verified=bool(verified),
                summary=verification.summary,
            )
            evidence_ids.append(item.evidence_id)
        return evidence_ids

    async def _refresh_assertion_evidence(
        self, contract: TaskContract, plan: tuple[Step, ...],
    ) -> bool:
        """Re-run read-only typed verifiers immediately before the success gate."""

        for step in plan:
            if not step.evidence_assertions:
                continue
            snapshot = await self.state_fusion.observe()
            skill = self.registry.get(step.skill)
            action = dict(skill.canonical_action(step.arguments))
            decision = self.policy.evaluate(contract, action)
            if not decision.allowed or decision.risk_class.value != "read_only":
                self.journal.transition(
                    contract.task_id, TaskState.FAILED,
                    reason="typed evidence finalization requires a read-only verifier",
                )
                return False
            action_id = "final_verify_" + hashlib.sha256(
                f"{contract.task_id}:{step.step_id}:{json.dumps(step.as_dict(), sort_keys=True, default=str)}".encode()
            ).hexdigest()[:24]
            context = SkillContext(
                task_id=contract.task_id, step_id=step.step_id, action_id=action_id,
                snapshot=snapshot, control=self.control,
                services={
                    **self.services,
                    "allowed_capabilities": contract.allowed_capabilities,
                    "allowed_domains": contract.allowed_domains,
                    "allowed_apps": contract.allowed_apps,
                },
            )
            try:
                preconditions = await skill.inspect(step.arguments, snapshot, context)
                if preconditions.get("ready") is False:
                    raise RuntimeError("final evidence precondition was not ready")
                prepared = await skill.prepare(step.arguments, snapshot, context)
                lease = self.control.acquire(
                    task_id=contract.task_id, action_id=action_id,
                    expected_human_epoch=snapshot.human_epoch,
                    expected_world_revision=snapshot.world_revision,
                )
                self._quiescent.clear()
                try:
                    raw = await skill.execute(prepared, lease, context)
                finally:
                    self.control.release(lease)
                    self._quiescent.set()
                refreshed_snapshot = await self.state_fusion.observe()
                verification = await skill.verify(
                    prepared, raw, refreshed_snapshot, context,
                )
                verification = self._verify_evidence_assertions(step, verification)
                evidence_ids = self._record_verification_evidence(
                    contract.task_id, step, "final_revalidation", verification,
                    refreshed_snapshot,
                )
                self.journal.append_event(contract.task_id, "EVIDENCE_REVALIDATED", {
                    "step_id": step.step_id, "evidence_ids": evidence_ids,
                    "verified": verification.verified,
                })
            except HumanPreempted:
                if self.journal.state(contract.task_id) == TaskState.RUNNING:
                    self.journal.transition(
                        contract.task_id, TaskState.PAUSED_BY_HUMAN,
                        reason="human preempted final evidence revalidation",
                    )
                return False
            except Exception as exc:
                self.journal.append_event(contract.task_id, "EVIDENCE_REVALIDATION_FAILED", {
                    "step_id": step.step_id, "error": type(exc).__name__,
                })
                self.journal.transition(
                    contract.task_id, TaskState.FAILED,
                    reason="final evidence revalidation failed",
                )
                return False
        return True

    @staticmethod
    def _verify_evidence_assertions(step: Step, verification: Verification) -> Verification:
        """Bind free-form criteria to allowlisted checks over trusted skill observations."""

        if not step.evidence_assertions:
            return verification
        results = evaluate_evidence_assertions(step.evidence_assertions, verification.observed)
        predicates = dict(verification.predicates)
        for predicate, result in results.items():
            predicates[predicate] = bool(verification.verified and result["verified"])
        assertions_verified = all(bool(item["verified"]) for item in results.values())
        summaries = "; ".join(str(item["summary"]) for item in results.values())
        return Verification(
            bool(verification.verified and assertions_verified), predicates,
            verification.observed,
            f"{verification.summary}; typed assertions: {summaries}",
        )

    @staticmethod
    def _step_assertion_references(step: Step) -> dict[str, tuple[str, ...]]:
        results = evaluate_evidence_assertions(step.evidence_assertions, {})
        return {
            predicate: (f"assertion:{result['spec_digest']}",)
            for predicate, result in results.items()
        }

    @classmethod
    def _plan_assertion_references(cls, plan: tuple[Step, ...]) -> dict[str, tuple[str, ...]]:
        references: dict[str, list[str]] = {}
        for step in plan:
            for predicate, values in cls._step_assertion_references(step).items():
                references.setdefault(predicate, []).extend(values)
        return {predicate: tuple(dict.fromkeys(values)) for predicate, values in references.items()}

    def _assert_idle_eligible(
        self, contract: TaskContract, state: TaskState, *, idle_seconds: float,
        visible: bool, capsule_running: bool,
    ) -> None:
        policy = contract.idle_resume_policy
        if not policy.enabled or state != TaskState.PAUSED_BY_HUMAN:
            raise IdleResumeDenied("idle resume is not explicitly enabled for this paused task")
        if idle_seconds < policy.idle_threshold_seconds:
            raise IdleResumeDenied("human idle threshold has not elapsed")
        if policy.expires_at is not None and time.time() >= policy.expires_at:
            raise IdleResumeDenied("idle resume grant expired")
        if not capsule_running or (policy.requires_visible_notice and not visible):
            raise IdleResumeDenied("capsule must be running and visible")
        if any(row["status"] in {"REQUESTED", "GRANTED"} for row in self._task_approval_rows(contract.task_id)):
            raise IdleResumeDenied("approval is pending or stale")
        plan = self._plans.get(contract.task_id) or self._load_plan(contract.task_id)
        index = int(self.journal.task(contract.task_id)["current_step"])
        if index >= len(plan):
            raise IdleResumeDenied("there is no authorized existing step to resume")
        step = plan[index]
        allowed = set(policy.allowed_steps_or_capabilities)
        skill = self.registry.get(step.skill)
        if allowed and step.step_id not in allowed and skill.definition.capability not in allowed:
            raise IdleResumeDenied("next step is outside the idle-resume grant")
        effect, risk = self.policy.classify(skill.canonical_action(step.arguments))
        if risk.value not in {"read_only", "local_reversible"}:
            raise IdleResumeDenied("idle resume cannot cross a high-risk commit boundary")

    def _resume_burst_limit(self, contract: TaskContract, task_id: str) -> int:
        # Explicit execution may complete the bounded plan. Idle continuation records
        # its burst in needs; conservative default remains one for an idle-enabled task.
        trigger = self.journal.get_meta(f"resume_trigger:{task_id}", "explicit")
        return contract.idle_resume_policy.maximum_resume_burst if trigger == "idle" else contract.max_steps

    def _effect_capable_task(self) -> str | None:
        for row in self.journal.active_tasks():
            if TaskState(row["state"]) in {
                TaskState.QUEUED, TaskState.COMPILING, TaskState.READY, TaskState.RUNNING,
                TaskState.RECONCILING, TaskState.PAUSED_BY_HUMAN, TaskState.WAITING_FOR_APPROVAL,
                TaskState.WAITING_FOR_HOST, TaskState.WAITING_FOR_USER,
            }:
                return str(row["task_id"])
        return None

    def _contract(self, task_id: str) -> TaskContract:
        if task_id not in self._contracts:
            self._contracts[task_id] = TaskContract.from_dict(self.journal.task(task_id)["contract"])
        return self._contracts[task_id]

    def _load_plan(self, task_id: str) -> tuple[Step, ...]:
        if task_id in self._plans:
            return self._plans[task_id]
        events = self.journal.events(task_id, limit=5000)
        plans = [event["payload"].get("steps") for event in events if event["event_type"] in {"PLAN_COMPILED", "PLAN_REVISED"}]
        if plans and plans[-1]:
            self._plans[task_id] = tuple(Step.from_dict(item, index) for index, item in enumerate(plans[-1]))
        return self._plans.get(task_id, ())

    @staticmethod
    def _normalize_plan(plan: tuple[Step, ...] | list[Mapping[str, Any]] | None) -> tuple[Step, ...]:
        if not plan:
            return ()
        if len(plan) > 1000:
            raise ValueError("plans are limited to 1000 steps")
        return tuple(item if isinstance(item, Step) else Step.from_dict(item, index) for index, item in enumerate(plan))

    def _validate_plan(self, contract: TaskContract, plan: tuple[Step, ...]) -> None:
        """Reject plans that cannot prove their contract before any durable mutation/effect."""

        if len(plan) > contract.max_steps:
            raise ValueError("plan length exceeds the task max_steps budget")
        step_ids = [step.step_id for step in plan]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("plan step_id values must be unique")
        covered: set[str] = set()
        for step in plan:
            skill = self.registry.get(step.skill)
            if step.skill == "browser.query":
                selector = str(step.arguments.get("selector") or "").strip().lower()
                if selector in {"", "*", "html", "body", ":root", "html body"}:
                    raise ValueError(
                        "browser.query requires a bounded semantic selector such as main; "
                        "document-wide selectors are not verifiable"
                    )
            native = set(skill.definition.expected_evidence)
            asserted = {assertion.predicate for assertion in step.evidence_assertions}
            if asserted and skill.definition.risk != "read_only":
                raise ValueError("typed evidence assertions require a read-only verifier skill")
            extra_assertions = asserted - set(step.success_predicates)
            if extra_assertions:
                raise ValueError("evidence assertions must bind declared step success predicates")
            unsupported = set(step.success_predicates) - native - asserted
            if unsupported:
                raise ValueError(
                    "step success predicates require native skill evidence or typed assertions: "
                    + ", ".join(sorted(unsupported))
                )
            for assertion in step.evidence_assertions:
                if assertion.predicate in native:
                    continue
                if assertion.operator in {"non_empty", "truthy"}:
                    raise ValueError(
                        "non-native success predicate needs value-bearing evidence: "
                        f"{assertion.operator} cannot prove {assertion.predicate!r}. Use "
                        "operator=contains/contains_all/contains_any/equals/count_at_least with an "
                        "'expected' value, and name that value verbatim in the predicate text. (To "
                        "simply author/open a file, skip drive_task: use workspace_write + browser_open.)"
                    )
                if not assertion.explicitly_represents_expected_facts():
                    raise ValueError(
                        f"non-native predicate {assertion.predicate!r} must literally contain each "
                        "asserted value: put every 'expected' string (every contains_any alternative, "
                        "and the count_at_least number) INTO the predicate text so it self-describes "
                        "what's being proven — e.g. predicate \"page contains 'Aegis' and 'AI security'\" "
                        "with contains_all expected ['Aegis','AI security']. (To simply author/open a "
                        "file, skip drive_task: use workspace_write + browser_open.)"
                    )
            covered.update(step.success_predicates)
        missing = set(contract.success_predicates) - covered
        if missing:
            raise ValueError(
                "plan does not cover every task success predicate: " + ", ".join(sorted(missing))
            )

    def _needs_host(self, contract: TaskContract) -> dict[str, Any]:
        return {
            "type": "NEEDS_HOST_REASONING",
            "goal": contract.goal,
            "constraints": contract.constraints,
            "contract_scope": {
                "allowed_capabilities": contract.allowed_capabilities,
                "allowed_domains": contract.allowed_domains,
                "allowed_apps": contract.allowed_apps,
                "risk_budget": contract.risk_budget,
            },
            "available_skills": [asdict(item) for item in self.registry.definitions()],
            # Grounding guidance: when a step needs an on-screen element, DON'T guess a
            # selector or x,y. Call ground_target with a natural-language intent to get a
            # short ranked candidate list (name/role/bounds/mark), then act on the chosen
            # element. Far more reliable than blind grounding (Prune4Web: 46.8%->88.28%).
            "grounding": {
                "tool": "ground_target",
                "when": "resolving any on-screen UI target for a click/type/select",
                "usage": "ground_target({intent: 'click the Save button', app_name?, top_k?}) "
                         "-> {candidates: [{mark, name, role, bounds, score}]}; pick a mark, "
                         "then act by name/role or bounds. Prefer this over guessing coordinates.",
            },
            "plan_schema": {
                "type": "array",
                "items": {
                    "step_id": "string", "skill": "registered skill", "arguments": "object",
                    "success_predicates": "non-empty string array",
                    "evidence_assertions": {
                        "type": "optional array",
                        "fields": ["predicate", "path", "operator", "expected"],
                        "operators": [
                            "equals", "contains", "contains_all", "contains_any",
                            "non_empty", "truthy", "count_at_least",
                        ],
                        "operator_signatures": {
                            "equals": "expected required",
                            "contains": "expected required",
                            "contains_all": "non-empty expected array required",
                            "contains_any": "non-empty expected array required",
                            "count_at_least": "integer expected required",
                            "non_empty": "expected forbidden",
                            "truthy": "expected forbidden",
                        },
                        "rule": (
                            "required for non-native predicates and must use a read-only verifier skill; "
                            "non_empty/truthy cannot prove non-native predicates, and every expected fact "
                            "must be literally represented in the predicate"
                        ),
                    },
                },
            },
            "verifier_observation_paths": {
                "browser.query": {
                    "matches": "array of matched semantic elements",
                    "matches.0.text": "text of the first matched element; use a selector that returns the intended bounded element",
                    "url": "current visible page URL when supplied by the adapter",
                },
                "workspace.inspect": {
                    "content": "bounded UTF-8 file content",
                    "path": "confined relative workspace path",
                    "sha256": "content digest when supplied by the adapter",
                },
            },
            "planning_rules": [
                "Use only exact names from available_skills[].name.",
                "Every step requires an explicit arguments object and a unique step_id.",
                "Mutating steps declare only their native expected_evidence predicate.",
                "Bind human-language task predicates on a later read-only verifier step with evidence_assertions.",
                "Every evidence_assertion.predicate must exactly equal one success_predicates entry on the same step.",
                "Use expected only for operators whose signature requires it; non_empty and truthy reject expected and cannot prove non-native task predicates.",
                "A non-native predicate must explicitly name every normalized expected fact; contains_any predicates name every alternative and count_at_least predicates name the numeric threshold.",
                "Workspace verifier path observations are confined relative paths matching the step arguments path.",
                "For browser.query text assertions use path matches.0.text, not matches.text or text.",
                "Use a bounded semantic browser.query selector such as main; body, html, :root, and * are rejected.",
                "Every task success predicate must be covered by at least one step.",
                "Plans must cover the complete contract, including desired artifacts and their read-back verifier, before execution begins.",
            ],
            "typed_evidence_example": [
                {
                    "step_id": "create-notes", "skill": "workspace.create",
                    "arguments": {"path": "notes.md", "content": "required marker"},
                    "success_predicates": ["artifact_created"],
                },
                {
                    "step_id": "verify-notes", "skill": "workspace.inspect",
                    "arguments": {"path": "notes.md"},
                    "success_predicates": ["notes.md contains required marker"],
                    "evidence_assertions": [{
                        "predicate": "notes.md contains required marker",
                        "path": "content", "operator": "contains", "expected": "required marker",
                    }],
                },
                {
                    "step_id": "verify-visible-page", "skill": "browser.query",
                    "arguments": {"selector": "main"},
                    "success_predicates": ["visible page contains required fact"],
                    "evidence_assertions": [{
                        "predicate": "visible page contains required fact",
                        "path": "matches.0.text", "operator": "contains", "expected": "required fact",
                    }],
                },
            ],
        }

    @staticmethod
    def _approval_preview(envelope: ActionEnvelope, decision: Any) -> dict[str, Any]:
        action = dict(envelope.action)
        canonical_action = redact(action)
        return {
            "effect": decision.effect_class.value,
            "risk": decision.risk_class.value,
            "target": action.get("path") or action.get("url") or action.get("app") or action.get("selector"),
            # Show the complete canonical action (argv/shell/cwd/environment and
            # handoff policy included for process effects) after secret
            # redaction. The digest is the same exact envelope binding signed by
            # ApprovalAuthority, not a second opaque summary hash.
            "action": canonical_action,
            "action_digest": envelope.digest,
            "reversible": decision.risk_class.value == "local_destructive" and False,
            "expected_outcome": action.get("expected"),
        }

    @staticmethod
    def _presentation_method(effect: str) -> str:
        if effect.startswith("workspace"):
            return "workspace"
        if effect.startswith("process"):
            return "process"
        if effect.startswith("browser") or effect in {"external_submit", "upload", "download"}:
            return "browser_semantic"
        if effect.startswith("app"):
            return "window"
        return "policy"

    def _last_sequence(self, task_id: str) -> int:
        events = self.journal.events(task_id, limit=5000)
        return int(events[-1]["sequence"]) if events else 0

    def _task_approval_rows(self, task_id: str) -> tuple[dict[str, Any], ...]:
        return tuple(
            self.journal.approval(event["payload"]["approval_id"])
            for event in self.journal.events(task_id, limit=5000)
            if event["event_type"] == "APPROVAL_REQUESTED" and event["payload"].get("approval_id")
        )

    async def _register_task_grant(self, contract: TaskContract) -> None:
        adapter = self.services.get("task_grants")
        if adapter is None:
            return
        result = adapter.register(contract.task_id, contract.allowed_domains)
        if hasattr(result, "__await__"):
            await result

    async def _revoke_task_grant(self, task_id: str) -> None:
        adapter = self.services.get("task_grants")
        if adapter is None:
            return
        result = adapter.revoke(task_id)
        if hasattr(result, "__await__"):
            await result

    async def _clear_task_grants(self) -> None:
        adapter = self.services.get("task_grants")
        if adapter is None:
            return
        result = adapter.clear()
        if hasattr(result, "__await__"):
            await result


class ActiveTaskConflict(RuntimeError):
    pass


class IdleResumeDenied(PermissionError):
    pass


class TaskAlreadyRunning(RuntimeError):
    pass
