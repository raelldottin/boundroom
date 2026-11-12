"""The knowledge service's configuration, and which platforms it may start (SPEC-007, RA-KNOW-007).

``docs/architecture/knowledge-loop-platforms.md`` ("Configuration") specifies it. The operator owns
``~/.config/boundroom/knowledge.json``. Its ``platforms`` list is empty unless the operator fills it,
and an entry turns on one platform in one workspace, with its own roles, channel allowlists,
answer audiences, share destinations, retention and credentials. Nothing here has a default that
connects: there is no built-in workspace, channel, service address or credential.

What this module guarantees:

- **Nothing starts by default (CP-1).** An absent, empty or refused file selects no platform, and
  ``load`` reads no environment variable and no secret file, ever. Tokens are read only by
  ``read_secret``, which an adapter's factory calls after ``start_platforms`` chose to build it.
- **Each platform on its own (CP-2).** Every entry becomes its own ``PlatformSelection``, with
  exactly the roles the operator wrote. Enabling Discord says nothing about Slack, and the reverse.
- **Slack capture waits on the terms decision (CP-7).** Slack's ``source`` role is refused unless
  the entry records the owner's decision on Slack's API terms and declares an internal app. Such an
  entry may also record the workspace's own message retention, ``workspace_retention_days``, so
  capture keeps nothing longer than the shorter of it and ``retention_days`` (SD-11).
- **No token in the configuration, its errors or its repr (CP-11).** Credentials are references,
  ``{"env": NAME}`` or ``{"file": /absolute/path}``. Anything shaped like a Slack, Discord or GitHub
  token is refused by where it is, never by what it is, and every model hides its input in errors.
- **The repository can only narrow.** A repository scope can turn a platform off, drop a role and
  remove channels, audiences or destinations, or shorten retention. It can't add a platform, a
  workspace, a role, a channel or a credential. A scope that tries is refused, and then nothing
  starts, because ignoring it would run wider than the repository asked.

Every refusal names its field and its reason. A refused file is refused whole: starting the
entries that did validate would run a configuration the operator never wrote.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal, Optional

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, SecretStr, StrictBool, StrictInt, ValidationError
from pydantic import field_validator, model_validator

from automation.knowledge.platform import (
    Place,
    PlatformId,
    PlatformKind,
    PlatformSelection,
    Principal,
    Role,
    Visibility,
)

DEFAULT_OPERATOR_FILE = Path("~/.config/boundroom/knowledge.json")

NonEmptyStr = Annotated[str, Field(min_length=1)]
ChannelSet = frozenset[PlatformId]

# Variables the knowledge service may never be pointed at: execution, GitHub, Kanban and supervisor
# credentials and settings (AGENTS.md, "Knowledge Loop on Discord or Slack"). BOUNDROOM_KNOWLEDGE_*
# stays open, because those are the service's own.
ENV_NAME_PATTERN = r"^[A-Z_][A-Z0-9_]{0,127}$"
FORBIDDEN_ENV_NAMES = re.compile(r"GITHUB|^GH_|^GIT_|KANBAN|^HERMES_|^REPO_AUTOMATION_|^OWLORY_|^BOUNDROOM_(?!KNOWLEDGE_)")

# Token shapes, matched anywhere in a string. Slack bot, user and app tokens, Discord bot tokens,
# GitHub tokens, and an HTTP bearer credential.
TOKEN_PATTERNS = (
    re.compile(r"xox[abeoprs]-[A-Za-z0-9-]{8,}"),
    re.compile(r"xapp-[0-9]+-[A-Za-z0-9-]{8,}"),
    re.compile(r"[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{27,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"(?i)\bbearer\s+\S"),
)

TOKEN_REASON = "looks like a token. Name the environment variable or secret file that holds it instead (SPEC-007 CP-11)"
BOUNDARY_REASON = (
    "the knowledge service has no path to run_next, a shell or agent, the supervisor's policy or queue, "
    "Kanban writes, GitHub writes or consumer sync, and holds no credential for them (AGENTS.md)"
)
STORED_TOKEN_REASON = (
    "the configuration never holds a token. Name the environment variable or secret file under credentials (SPEC-007 CP-11)"
)
ADDRESS_REASON = (
    "adapters connect outbound through the platform's own gateway or Socket Mode, "
    "so the configuration names no service address or webhook (SPEC-007)"
)

# Why each field the operator's file may not hold is refused, by where it would appear. A typo is
# refused by ``extra="forbid"`` too; these get a reason because each one is a step across a boundary.
TOP_LEVEL_REFUSALS: dict[str, str] = {
    name: BOUNDARY_REASON
    for name in (
        "agent",
        "agent_command",
        "commands",
        "execution",
        "github",
        "github_token",
        "kanban",
        "queue",
        "run_next",
        "shell",
        "supervisor",
        "sync",
    )
}
PLATFORM_REFUSALS: dict[str, str] = {
    **{name: STORED_TOKEN_REASON for name in ("token", "tokens", "bot_token", "app_token", "secret", "secrets", "password")},
    **{name: ADDRESS_REASON for name in ("endpoint", "url", "webhook", "webhook_url")},
    **{name: BOUNDARY_REASON for name in ("github", "github_token", "kanban", "run_next", "shell", "commands")},
}
CREDENTIAL_NAMES: dict[str, frozenset[str]] = {
    # Discord needs one bot token. Slack's Socket Mode needs the bot token and an app-level token.
    "discord": frozenset({"bot_token"}),
    "slack": frozenset({"bot_token", "app_token"}),
}
REPOSITORY_TOP_LEVEL_REFUSALS: dict[str, str] = {
    **TOP_LEVEL_REFUSALS,
    "identity_links": "identity links are operator-maintained records. The repository can't add or change one (SPEC-007)",
}
REPOSITORY_PLATFORM_REFUSALS: dict[str, str] = {
    **PLATFORM_REFUSALS,
    "credentials": "the repository can't hold or name a credential (SPEC-007, Configuration)",
    "app": "the repository can't declare the Slack app. Only the operator can (SPEC-007 CP-7)",
    "slack_terms_decision": "the repository can't record the owner's Slack terms decision (SPEC-007 CP-7)",
    "workspace_retention_days": (
        "the workspace's own retention is the operator's record of the workspace. "
        "The repository can shorten retention_days instead (SPEC-007 part 2, SD-11)"
    ),
}


class KnowledgeConfigError(Exception):
    """The configuration was refused. The message names the field and the reason, never a value."""


class SecretUnavailable(KnowledgeConfigError):
    """A secret reference names a variable or file that holds nothing."""


class Config(BaseModel):
    """Operator and repository configuration. Unknown keys are refused, and inputs never reach errors.

    Validation is not ``strict`` because JSON arrays become sets and tuples here, and a model-level
    check makes pydantic validate the parsed dict in Python mode. Scalars are strict by type instead.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


def _refuse(data: Any, refusals: Mapping[str, str]) -> Any:
    if isinstance(data, dict):
        for name, reason in refusals.items():
            if name in data:
                raise ValueError(f"{name}: {reason}")
    return data


def _iso_date(value: Any) -> date:
    # Lax pydantic would read a number as a Unix timestamp, so a record date must be written out.
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("must be a date written as YYYY-MM-DD")
    return date.fromisoformat(value)


RecordDate = Annotated[date, BeforeValidator(_iso_date)]


class SecretRef(Config):
    """Where a token lives: an environment variable's name or a secret file's path. Never the token."""

    env: Optional[Annotated[str, Field(pattern=ENV_NAME_PATTERN)]] = None
    file: Optional[NonEmptyStr] = None

    @field_validator("env")
    @classmethod
    def _env(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and FORBIDDEN_ENV_NAMES.search(value):
            raise ValueError(f"{value} is not the knowledge service's variable: {BOUNDARY_REASON}")
        return value

    @field_validator("file")
    @classmethod
    def _file(cls, value: Optional[str]) -> Optional[str]:
        # A secret store or a service manager mounts the file, outside any checkout. A relative path
        # would resolve inside whatever directory the service started in, which may be a repository.
        if value is not None:
            path = PurePosixPath(value)
            if not path.is_absolute() or ".." in path.parts or "\x00" in value:
                raise ValueError("a secret file must be an absolute path with no '..'")
        return value

    @model_validator(mode="after")
    def _one(self) -> SecretRef:
        if (self.env is None) == (self.file is None):
            raise ValueError("a secret reference names exactly one of env or file")
        return self

    @property
    def name(self) -> str:
        """What to tell the operator: which variable or file, never what it holds."""
        return f"environment variable {self.env}" if self.env is not None else f"secret file {self.file}"


class Credentials(Config):
    bot_token: Optional[SecretRef] = None
    app_token: Optional[SecretRef] = None

    @model_validator(mode="before")
    @classmethod
    def _only_platform_tokens(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for name in data:
                if name not in ("bot_token", "app_token"):
                    raise ValueError(f"{name}: the knowledge service holds only its own platform tokens. {BOUNDARY_REASON}")
        return data


class ChannelAllowlists(Config):
    """The channels each role may act in. Empty unless the operator lists them."""

    interface: ChannelSet = frozenset()
    source: ChannelSet = frozenset()


class ShareDestination(Config):
    channel: PlatformId
    thread: Optional[PlatformId] = None


class SlackTermsDecision(Config):
    """Where the owner recorded how Slack's API terms apply to capturing this workspace (decision 5).

    The loader can check that a decision is recorded, not that it is right: reading the terms is the
    owner's call (SPEC-007, open decision 2).
    """

    record: NonEmptyStr
    recorded_by: NonEmptyStr
    recorded_at: RecordDate


class PlatformConfig(Config):
    """One platform the operator enabled, in one workspace."""

    kind: PlatformKind
    workspace: PlatformId
    # Interface without source is the default: asking needs far fewer permissions than capturing.
    roles: Annotated[frozenset[Role], Field(min_length=1)] = frozenset({"interface"})
    channels: ChannelAllowlists = ChannelAllowlists()
    # Who may read an answer: only the asker (ephemeral), or the thread it was asked in.
    answer_audiences: Annotated[frozenset[Visibility], Field(min_length=1)] = frozenset({"ephemeral"})
    share_destinations: frozenset[ShareDestination] = frozenset()
    retention_days: Optional[Annotated[StrictInt, Field(ge=1)]] = None
    credentials: Credentials
    app: Optional[Literal["internal", "distributed"]] = None
    slack_terms_decision: Optional[SlackTermsDecision] = None
    # The Slack workspace's own message retention, as its admins set it. Slack offers no API to read
    # it outside Enterprise Grid, which is out of scope, so the operator records it. Absent means the
    # workspace keeps messages indefinitely, and ``retention_days`` alone bounds capture.
    workspace_retention_days: Optional[Annotated[StrictInt, Field(ge=1)]] = None

    @model_validator(mode="before")
    @classmethod
    def _refused(cls, data: Any) -> Any:
        return _refuse(data, PLATFORM_REFUSALS)

    @model_validator(mode="after")
    def _check(self) -> PlatformConfig:
        where = f"{self.kind} workspace {self.workspace}"
        # A field for a role that isn't enabled would do nothing, and an operator reading it would
        # think it does.
        for role in ("interface", "source"):
            allowed = getattr(self.channels, role)
            if role in self.roles and not allowed:
                raise ValueError(f"channels.{role}: the {role} role on {where} needs at least one allowlisted channel")
            if role not in self.roles and allowed:
                raise ValueError(f"channels.{role}: the {role} role is not enabled on {where}")
        if "interface" not in self.roles:
            if "answer_audiences" in self.model_fields_set:
                raise ValueError(f"answer_audiences: the interface role is not enabled on {where}")
            if self.share_destinations:
                raise ValueError(f"share_destinations: the interface role is not enabled on {where}")
        if "source" in self.roles and self.retention_days is None:
            raise ValueError(f"retention_days: the source role on {where} needs a retention; captured evidence has no default")
        if "source" not in self.roles and self.retention_days is not None:
            raise ValueError(f"retention_days: the source role is not enabled on {where}")

        needed = CREDENTIAL_NAMES[self.kind]
        for name in ("bot_token", "app_token"):
            present = getattr(self.credentials, name) is not None
            if name in needed and not present:
                raise ValueError(f"credentials.{name}: {where} isn't enabled without it")
            if name not in needed and present:
                raise ValueError(f"credentials.{name}: {self.kind} doesn't use one")

        if self.workspace_retention_days is not None:
            if self.kind != "slack":
                raise ValueError("workspace_retention_days: only a Slack entry records its workspace's retention")
            if "source" not in self.roles:
                raise ValueError(f"workspace_retention_days: the source role is not enabled on {where}")
        if self.kind == "discord":
            if self.app is not None:
                raise ValueError("app: only a Slack entry declares its app type")
            if self.slack_terms_decision is not None:
                raise ValueError("slack_terms_decision: only a Slack entry records it")
        elif "source" in self.roles:
            # CP-7, and part 2's decision 1: an internal app in the operator's own workspace, after the
            # owner records how the terms apply. A distributed app would also be held to 1 history
            # request a minute.
            if self.slack_terms_decision is None:
                raise ValueError(
                    f"roles: Slack capture on {where} waits on the owner's recorded decision on Slack's API terms "
                    "(slack_terms_decision) (SPEC-007 CP-7)"
                )
            if self.app != "internal":
                raise ValueError(
                    f'app: Slack capture on {where} needs an internal app, declared as "app": "internal" (SPEC-007 CP-7)'
                )
        return self

    def selection(self) -> PlatformSelection:
        return PlatformSelection(kind=self.kind, workspace=self.workspace, roles=self.roles)

    def destination(self, share: ShareDestination) -> Place:
        return Place(platform=self.kind, workspace=self.workspace, channel=share.channel, thread=share.thread)

    @property
    def capture_retention_days(self) -> Optional[int]:
        """How long captured evidence may be kept: the shorter of the operator's and the workspace's (SD-11)."""
        days = [d for d in (self.retention_days, self.workspace_retention_days) if d is not None]
        return min(days) if days else None

    def secrets(self) -> dict[str, SecretRef]:
        return {name: ref for name in ("bot_token", "app_token") if (ref := getattr(self.credentials, name)) is not None}


class LinkedUser(Config):
    workspace: PlatformId
    # A platform user id. Names and email addresses don't fit the id pattern, so a link can't be
    # written as a match on either (SPEC-007: links are never inferred).
    user: PlatformId


class IdentityLink(Config):
    """An operator-maintained record that one Discord user and one Slack user are the same person.

    A link alone shares nothing: an answer still needs a reviewed share for that destination
    (decision 4, RA-KNOW-010).
    """

    discord: LinkedUser
    slack: LinkedUser
    recorded_by: NonEmptyStr
    recorded_at: RecordDate

    def principals(self) -> tuple[Principal, Principal]:
        return (
            Principal(platform="discord", workspace=self.discord.workspace, user=self.discord.user),
            Principal(platform="slack", workspace=self.slack.workspace, user=self.slack.user),
        )


class KnowledgeConfig(Config):
    """``~/.config/boundroom/knowledge.json``. Empty means no platform is enabled."""

    platforms: tuple[PlatformConfig, ...] = ()
    identity_links: tuple[IdentityLink, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _refused(cls, data: Any) -> Any:
        return _refuse(data, TOP_LEVEL_REFUSALS)

    @model_validator(mode="after")
    def _check(self) -> KnowledgeConfig:
        seen: set[tuple[str, str]] = set()
        for platform in self.platforms:
            key = (platform.kind, platform.workspace)
            if key in seen:
                raise ValueError(f"platforms: {platform.kind} workspace {platform.workspace} is configured twice")
            seen.add(key)
        for index, link in enumerate(self.identity_links):
            for kind, linked in (("discord", link.discord), ("slack", link.slack)):
                if (kind, linked.workspace) not in seen:
                    raise ValueError(f"identity_links.{index}: {kind} workspace {linked.workspace} is not configured")
        if len(set(self.identity_links)) != len(self.identity_links):
            raise ValueError("identity_links: a link is recorded twice")
        return self

    def selections(self) -> tuple[PlatformSelection, ...]:
        return tuple(platform.selection() for platform in self.platforms)

    def platform(self, selection: PlatformSelection) -> PlatformConfig:
        for platform in self.platforms:
            if (platform.kind, platform.workspace) == (selection.kind, selection.workspace):
                return platform
        raise KnowledgeConfigError(f"{selection.kind} workspace {selection.workspace} is not configured")


# The repository's scope: narrow-only (SPEC-007 "The repository can't enable a platform"; SPEC-008).


class RepositoryPlatformScope(Config):
    """What a repository asks of one operator-configured platform. Every field may only narrow."""

    kind: PlatformKind
    workspace: PlatformId
    enabled: Optional[StrictBool] = None
    roles: Optional[Annotated[frozenset[Role], Field(min_length=1)]] = None
    channels: Optional[ChannelAllowlists] = None
    answer_audiences: Optional[Annotated[frozenset[Visibility], Field(min_length=1)]] = None
    share_destinations: Optional[frozenset[ShareDestination]] = None
    retention_days: Optional[Annotated[StrictInt, Field(ge=1)]] = None

    @model_validator(mode="before")
    @classmethod
    def _refused(cls, data: Any) -> Any:
        _refuse(data, REPOSITORY_PLATFORM_REFUSALS)
        if isinstance(data, dict) and data.get("enabled", False) is not False:
            raise ValueError("enabled: the repository can only turn a platform off (SPEC-007, Configuration)")
        return data


class RepositoryKnowledgeScope(Config):
    platforms: tuple[RepositoryPlatformScope, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _refused(cls, data: Any) -> Any:
        return _refuse(data, REPOSITORY_TOP_LEVEL_REFUSALS)


def _subset(asked: Iterable[Any], allowed: Iterable[Any], what: str, label: Any = str) -> None:
    extra = set(asked) - set(allowed)
    if extra:
        names = ", ".join(sorted(label(item) for item in extra))
        raise KnowledgeConfigError(f"{what}: the repository can't add {names} (SPEC-007, Configuration)")


def narrow(config: KnowledgeConfig, scope: RepositoryKnowledgeScope) -> KnowledgeConfig:
    """``config`` reduced by the repository's ``scope``. Anything that would widen it is refused."""
    by_key = {(platform.kind, platform.workspace): platform for platform in config.platforms}
    asked: dict[tuple[str, str], RepositoryPlatformScope] = {}
    for index, item in enumerate(scope.platforms):
        key = (item.kind, item.workspace)
        where = f"platforms.{index}"
        if key not in by_key:
            raise KnowledgeConfigError(
                f"{where}: {item.kind} workspace {item.workspace} is not in the operator's configuration, "
                "and the repository can't add a platform or a workspace (SPEC-007, Configuration)"
            )
        if key in asked:
            raise KnowledgeConfigError(f"{where}: {item.kind} workspace {item.workspace} is scoped twice")
        asked[key] = item

    kept: list[PlatformConfig] = []
    for key, platform in by_key.items():
        item = asked.get(key)
        if item is None:
            kept.append(platform)
            continue
        if item.enabled is False:
            continue
        where = f"{platform.kind} workspace {platform.workspace}"
        roles = item.roles if item.roles is not None else platform.roles
        _subset(roles, platform.roles, f"roles on {where}")
        channels = {}
        for role in ("interface", "source"):
            operator_channels = getattr(platform.channels, role)
            wanted = (
                getattr(item.channels, role) if item.channels is not None and role in item.channels.model_fields_set else None
            )
            if wanted is not None:
                _subset(wanted, operator_channels, f"channels.{role} on {where}")
            # A dropped role takes its allowlist with it, so the narrowed entry validates as written.
            channels[role] = (wanted if wanted is not None else operator_channels) if role in roles else frozenset()
        audiences = item.answer_audiences if item.answer_audiences is not None else platform.answer_audiences
        _subset(audiences, platform.answer_audiences, f"answer_audiences on {where}")
        shares = item.share_destinations if item.share_destinations is not None else platform.share_destinations
        _subset(shares, platform.share_destinations, f"share_destinations on {where}", label=_share_label)
        retention = platform.retention_days
        if item.retention_days is not None:
            if retention is None or item.retention_days > retention:
                raise KnowledgeConfigError(f"retention_days on {where}: the repository can only shorten retention")
            retention = item.retention_days
        update: dict[str, Any] = {
            "roles": roles,
            "channels": ChannelAllowlists(**channels),
            "retention_days": retention if "source" in roles else None,
            # The workspace's retention is recorded only for capture, so it goes with the source role.
            "workspace_retention_days": platform.workspace_retention_days if "source" in roles else None,
        }
        # Interface-only fields go with the interface role, as they would in an operator's entry.
        if "interface" in roles:
            update |= {"answer_audiences": audiences, "share_destinations": shares}
        dropped = set(update) | {"answer_audiences", "share_destinations"}
        try:
            kept.append(PlatformConfig.model_validate({**platform.model_dump(mode="json", exclude=dropped), **update}))
        except ValidationError as error:
            raise KnowledgeConfigError("; ".join(f"repository scope for {where}: {line}" for line in _reasons(error))) from None

    enabled = {(platform.kind, platform.workspace) for platform in kept}
    links = tuple(
        link
        for link in config.identity_links
        if ("discord", link.discord.workspace) in enabled and ("slack", link.slack.workspace) in enabled
    )
    return KnowledgeConfig(platforms=tuple(kept), identity_links=links)


def _share_label(share: ShareDestination) -> str:
    return f"destination {share.channel}" + (f" thread {share.thread}" if share.thread else "")


# Loading. Nothing below reads an environment variable or a secret file.


def token_locations(data: Any, path: str = "") -> Iterator[str]:
    """Where in parsed JSON a string is shaped like a token: key paths only, never the string."""
    if isinstance(data, dict):
        for key, value in data.items():
            here = f"{path}.{key}" if path else str(key)
            if _looks_like_token(str(key)):
                yield f"{path or '(top level)'}: a key"
            yield from token_locations(value, here)
    elif isinstance(data, list):
        for index, value in enumerate(data):
            yield from token_locations(value, f"{path}.{index}" if path else str(index))
    elif isinstance(data, str) and _looks_like_token(data):
        yield path or "(top level)"


def _looks_like_token(text: str) -> bool:
    return any(pattern.search(text) for pattern in TOKEN_PATTERNS)


def _reasons(error: ValidationError) -> list[str]:
    reasons = []
    for item in error.errors(include_url=False, include_context=False, include_input=False):
        location = ".".join(str(part) for part in item["loc"])
        message = item["msg"].removeprefix("Value error, ")
        reasons.append(f"{location}: {message}" if location else message)
    return reasons


def parse(model: type[Config], data: Any) -> Any:
    """Validate parsed JSON, refusing a token-shaped string before pydantic sees any of it."""
    found = list(token_locations(data))
    if found:
        raise KnowledgeConfigError("; ".join(f"{where}: {TOKEN_REASON}" for where in found))
    try:
        return model.model_validate(data)
    except ValidationError as error:
        raise KnowledgeConfigError("; ".join(_reasons(error))) from None


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as error:
        # A JSON error names a line and column, and an OS error names the path: neither shows content.
        first = str(error).splitlines()[0] if str(error) else type(error).__name__
        raise KnowledgeConfigError(f"can't read it: {first}") from None


@dataclass(frozen=True)
class KnowledgeSettings:
    """What the knowledge service may start, and why anything was refused."""

    config: KnowledgeConfig = field(default_factory=KnowledgeConfig)
    refusals: tuple[str, ...] = ()

    @property
    def selections(self) -> tuple[PlatformSelection, ...]:
        """What to hand ``start_platforms``. Empty when nothing is enabled or anything was refused."""
        return self.config.selections()

    def platform(self, selection: PlatformSelection) -> PlatformConfig:
        """The entry behind a selection, for the adapter factory that builds it."""
        return self.config.platform(selection)


def load(
    operator_file: Optional[Path] = DEFAULT_OPERATOR_FILE,
    repository_file: Optional[Path] = None,
) -> KnowledgeSettings:
    """The operator's configuration, narrowed by the repository's scope if there is one.

    An absent operator file enables nothing and is not a problem. A refused file, or a refused
    repository scope, enables nothing and says why.
    """
    if operator_file is None:
        return KnowledgeSettings()
    operator_file = operator_file.expanduser()
    if not operator_file.exists():
        return KnowledgeSettings()
    try:
        config = parse(KnowledgeConfig, _read_json(operator_file))
    except KnowledgeConfigError as error:
        return KnowledgeSettings(refusals=(f"knowledge: refusing {operator_file}: {error}",))
    if repository_file is None or not repository_file.exists():
        return KnowledgeSettings(config=config)
    try:
        config = narrow(config, parse(RepositoryKnowledgeScope, _read_json(repository_file)))
    except KnowledgeConfigError as error:
        return KnowledgeSettings(refusals=(f"knowledge: refusing {repository_file}, so no platform starts: {error}",))
    return KnowledgeSettings(config=config)


def read_secret(ref: SecretRef, environ: Optional[Mapping[str, str]] = None) -> SecretStr:
    """Read one token, for an adapter's factory only. The value stays wrapped, so no repr shows it."""
    if ref.env is not None:
        value = (os.environ if environ is None else environ).get(ref.env, "")
    else:
        try:
            value = Path(str(ref.file)).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            value = ""
    if not value:
        raise SecretUnavailable(f"{ref.name} holds no token")
    return SecretStr(value)
