"""Runtime-neutral thread, turn, and attachment identity lifecycle."""

from dataclasses import dataclass, replace
from enum import StrEnum
from uuid import UUID, uuid4, uuid5

_IDENTITY_NAMESPACE = UUID("409330c4-edc3-4f31-8793-530914c9cc37")


@dataclass(frozen=True, order=True)
class ThreadId:
    value: str


@dataclass(frozen=True, order=True)
class TurnId:
    value: str


@dataclass(frozen=True, order=True)
class AttachmentId:
    value: str


class ThreadStatus(StrEnum):
    ACTIVE = "active"
    DORMANT = "dormant"


class TurnStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"


class AttachmentStatus(StrEnum):
    ACTIVE = "active"
    CLOSED = "closed"


@dataclass(frozen=True)
class IdentityProvenance:
    agent: str
    agent_thread_id: str | None = None
    agent_turn_id: str | None = None
    agent_attachment_id: str | None = None
    legacy_session_id: str | None = None


@dataclass(frozen=True)
class RuntimeIdentity:
    """An explicit address; unknown dimensions remain None instead of being inferred."""

    thread_id: ThreadId | None
    turn_id: TurnId | None
    attachment_id: AttachmentId | None
    provenance: IdentityProvenance

    @classmethod
    def legacy_hook(cls, agent: str, session_id: str | None) -> "RuntimeIdentity":
        """Represent Hook-era identity without pretending a session is a thread or turn."""

        return cls(
            thread_id=None,
            turn_id=None,
            attachment_id=None,
            provenance=IdentityProvenance(agent=agent, legacy_session_id=session_id),
        )


@dataclass(frozen=True)
class AgentThread:
    id: ThreadId
    status: ThreadStatus
    provenance: IdentityProvenance


@dataclass(frozen=True)
class Turn:
    id: TurnId
    thread_id: ThreadId
    attachment_id: AttachmentId | None
    status: TurnStatus
    observed_start: bool
    provenance: IdentityProvenance


@dataclass(frozen=True)
class RuntimeAttachment:
    id: AttachmentId
    thread_id: ThreadId
    status: AttachmentStatus
    provenance: IdentityProvenance


class IdentityError(ValueError):
    """Base error for invalid or ambiguous runtime identity."""


class MissingRuntimeIdentity(IdentityError):
    """An event omitted an identity required for safe routing."""


class UnknownRuntimeIdentity(IdentityError):
    """An event referenced an identity that hasn't been observed."""


class ConflictingRuntimeIdentity(IdentityError):
    """Events disagree about an existing identity or terminal state."""


class RuntimeIdentityRegistry:
    """Isolate concurrent runtime identities and reconcile duplicate/out-of-order events."""

    def __init__(self) -> None:
        self._threads: dict[ThreadId, IdentityProvenance] = {}
        self._threads_by_agent_id: dict[tuple[str, str], ThreadId] = {}
        self._turns: dict[TurnId, Turn] = {}
        self._turns_by_agent_id: dict[tuple[ThreadId, str], TurnId] = {}
        self._attachments: dict[AttachmentId, RuntimeAttachment] = {}
        self._attachments_by_agent_id: dict[tuple[ThreadId, str], AttachmentId] = {}

    def observe_thread(self, agent: str, agent_thread_id: str) -> AgentThread:
        agent = _required(agent, "agent")
        agent_thread_id = _required(agent_thread_id, "agent thread id")
        key = (agent, agent_thread_id)
        thread_id = self._threads_by_agent_id.get(key)
        if thread_id is None:
            thread_id = ThreadId(_stable_id("thread", agent, agent_thread_id))
            provenance = IdentityProvenance(agent=agent, agent_thread_id=agent_thread_id)
            self._threads[thread_id] = provenance
            self._threads_by_agent_id[key] = thread_id
        return self.thread(thread_id)

    def resolve_thread(self, agent: str, agent_thread_id: str) -> AgentThread:
        key = (_required(agent, "agent"), _required(agent_thread_id, "agent thread id"))
        thread_id = self._threads_by_agent_id.get(key)
        if thread_id is None:
            raise UnknownRuntimeIdentity(f"unknown agent thread: {key[0]}:{key[1]}")
        return self.thread(thread_id)

    def thread(self, thread_id: ThreadId) -> AgentThread:
        provenance = self._threads.get(thread_id)
        if provenance is None:
            raise UnknownRuntimeIdentity(f"unknown thread: {thread_id.value}")
        is_active = any(
            attachment.thread_id == thread_id and attachment.status == AttachmentStatus.ACTIVE
            for attachment in self._attachments.values()
        ) or any(
            turn.thread_id == thread_id and turn.status == TurnStatus.ACTIVE
            for turn in self._turns.values()
        )
        return AgentThread(
            id=thread_id,
            status=ThreadStatus.ACTIVE if is_active else ThreadStatus.DORMANT,
            provenance=provenance,
        )

    def attach(
        self, thread_id: ThreadId, *, agent_attachment_id: str | None = None
    ) -> RuntimeAttachment:
        thread = self.thread(thread_id)
        if agent_attachment_id is not None:
            agent_attachment_id = _required(agent_attachment_id, "agent attachment id")
            key = (thread_id, agent_attachment_id)
            attachment_id = self._attachments_by_agent_id.get(key)
            if attachment_id is not None:
                return self._attachments[attachment_id]
            attachment_id = AttachmentId(
                _stable_id("attachment", thread_id.value, agent_attachment_id)
            )
            self._attachments_by_agent_id[key] = attachment_id
        else:
            attachment_id = AttachmentId(uuid4().hex)
        attachment = RuntimeAttachment(
            id=attachment_id,
            thread_id=thread_id,
            status=AttachmentStatus.ACTIVE,
            provenance=IdentityProvenance(
                agent=thread.provenance.agent,
                agent_thread_id=thread.provenance.agent_thread_id,
                agent_attachment_id=agent_attachment_id,
            ),
        )
        self._attachments[attachment_id] = attachment
        return attachment

    def detach(self, attachment_id: AttachmentId) -> RuntimeAttachment:
        attachment = self._attachments.get(attachment_id)
        if attachment is None:
            raise UnknownRuntimeIdentity(f"unknown attachment: {attachment_id.value}")
        if attachment.status == AttachmentStatus.CLOSED:
            return attachment
        attachment = replace(attachment, status=AttachmentStatus.CLOSED)
        self._attachments[attachment_id] = attachment
        return attachment

    def start_turn(
        self,
        thread_id: ThreadId,
        agent_turn_id: str,
        *,
        attachment_id: AttachmentId | None = None,
    ) -> Turn:
        thread = self.thread(thread_id)
        agent_turn_id = _required(agent_turn_id, "agent turn id")
        if attachment_id is not None:
            attachment = self._attachments.get(attachment_id)
            if attachment is None:
                raise UnknownRuntimeIdentity(f"unknown attachment: {attachment_id.value}")
            if attachment.thread_id != thread_id:
                raise ConflictingRuntimeIdentity("attachment belongs to another thread")

        key = (thread_id, agent_turn_id)
        turn_id = self._turns_by_agent_id.get(key)
        if turn_id is not None:
            turn = self._turns[turn_id]
            if (
                attachment_id is not None
                and turn.attachment_id is not None
                and turn.attachment_id != attachment_id
            ):
                raise ConflictingRuntimeIdentity("turn belongs to another attachment")
            turn = replace(
                turn,
                attachment_id=turn.attachment_id or attachment_id,
                observed_start=True,
            )
            self._turns[turn_id] = turn
            return turn

        turn_id = TurnId(_stable_id("turn", thread_id.value, agent_turn_id))
        turn = Turn(
            id=turn_id,
            thread_id=thread_id,
            attachment_id=attachment_id,
            status=TurnStatus.ACTIVE,
            observed_start=True,
            provenance=IdentityProvenance(
                agent=thread.provenance.agent,
                agent_thread_id=thread.provenance.agent_thread_id,
                agent_turn_id=agent_turn_id,
                agent_attachment_id=(
                    self._attachments[attachment_id].provenance.agent_attachment_id
                    if attachment_id is not None
                    else None
                ),
            ),
        )
        self._turns[turn_id] = turn
        self._turns_by_agent_id[key] = turn_id
        return turn

    def finish_turn(self, thread_id: ThreadId, agent_turn_id: str, status: TurnStatus) -> Turn:
        if status == TurnStatus.ACTIVE:
            raise ValueError("finish status must be completed or interrupted")
        thread = self.thread(thread_id)
        agent_turn_id = _required(agent_turn_id, "agent turn id")
        key = (thread_id, agent_turn_id)
        turn_id = self._turns_by_agent_id.get(key)
        if turn_id is None:
            turn_id = TurnId(_stable_id("turn", thread_id.value, agent_turn_id))
            turn = Turn(
                id=turn_id,
                thread_id=thread_id,
                attachment_id=None,
                status=status,
                observed_start=False,
                provenance=IdentityProvenance(
                    agent=thread.provenance.agent,
                    agent_thread_id=thread.provenance.agent_thread_id,
                    agent_turn_id=agent_turn_id,
                ),
            )
            self._turns[turn_id] = turn
            self._turns_by_agent_id[key] = turn_id
            return turn

        turn = self._turns[turn_id]
        if turn.status != TurnStatus.ACTIVE and turn.status != status:
            raise ConflictingRuntimeIdentity(
                f"turn already ended as {turn.status}; cannot change to {status}"
            )
        turn = replace(turn, status=status)
        self._turns[turn_id] = turn
        return turn

    def turn(self, turn_id: TurnId) -> Turn:
        turn = self._turns.get(turn_id)
        if turn is None:
            raise UnknownRuntimeIdentity(f"unknown turn: {turn_id.value}")
        return turn

    def active_turns(self, thread_id: ThreadId) -> tuple[Turn, ...]:
        self.thread(thread_id)
        return tuple(
            turn
            for turn in self._turns.values()
            if turn.thread_id == thread_id and turn.status == TurnStatus.ACTIVE
        )

    def address_turn(self, turn_id: TurnId) -> RuntimeIdentity:
        turn = self.turn(turn_id)
        return RuntimeIdentity(
            thread_id=turn.thread_id,
            turn_id=turn.id,
            attachment_id=turn.attachment_id,
            provenance=turn.provenance,
        )


def _required(value: str, name: str) -> str:
    if not value.strip():
        raise MissingRuntimeIdentity(f"missing {name}")
    return value


def _stable_id(kind: str, *parts: str) -> str:
    return uuid5(_IDENTITY_NAMESPACE, "\0".join((kind, *parts))).hex
