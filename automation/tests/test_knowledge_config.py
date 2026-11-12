"""The knowledge service's configuration and loader (SPEC-007, RA-KNOW-007).

Covers CP-1 (nothing is enabled by default, and loading reads no token), CP-2 (each platform is
enabled on its own, end to end through ``start_platforms``), CP-7 (Slack capture waits on the owner's
terms decision and an internal app) and CP-11 (credentials are references, and no token reaches a
refusal, a repr or the test output). Then the repository's narrow-only rule, identity links, and the
named reason for every refused field.

Planted markers stand in for real tokens. Each CP-11 test checks that its marker appears nowhere the
configuration can print.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator, MutableMapping
from pathlib import Path
from typing import Any, Optional

import pytest

from automation.knowledge.config import (
    DEFAULT_OPERATOR_FILE,
    Config,
    KnowledgeConfig,
    KnowledgeConfigError,
    KnowledgeSettings,
    RepositoryKnowledgeScope,
    SecretRef,
    SecretUnavailable,
    load,
    narrow,
    parse,
    read_secret,
)
from automation.knowledge.platform import (
    ChatPlatform,
    FakePlatform,
    Place,
    PlatformKind,
    PlatformSelection,
    Principal,
    RoleNotEnabled,
    start_platforms,
)

SLACK_WS = "T0FAKE0001"
DISCORD_WS = "1100000000000000001"
SLACK_MARKER = "xoxb-0000PLANTED-MARKER-do-not-print"
APP_MARKER = "xapp-1-A0PLANTED-MARKER-do-not-print"
DISCORD_MARKER = "MTEwMDAwMDAwMDAwMDAwMDAx.GPLANT.PLANTED-MARKER-do-not-print-0000000"
GITHUB_MARKER = "ghp_PLANTEDMARKERdonotprint000000000000"
# Not token-shaped: what an environment variable or secret file actually holds in these tests.
VALUE_MARKER = "PLANTED-SECRET-VALUE-do-not-print"
MARKERS = (SLACK_MARKER, APP_MARKER, DISCORD_MARKER, GITHUB_MARKER, VALUE_MARKER)

DECISION = {"record": "docs/decisions/slack-capture.md", "recorded_by": "the owner", "recorded_at": "2026-11-12"}


def slack(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "kind": "slack",
        "workspace": SLACK_WS,
        "channels": {"interface": ["C0FAKE0001"]},
        "credentials": {
            "bot_token": {"env": "BOUNDROOM_KNOWLEDGE_SLACK_BOT_TOKEN"},
            "app_token": {"env": "BOUNDROOM_KNOWLEDGE_SLACK_APP_TOKEN"},
        },
    }
    entry.update(overrides)
    return entry


def slack_capture(**overrides: Any) -> dict[str, Any]:
    capture: dict[str, Any] = {
        "roles": ["interface", "source"],
        "channels": {"interface": ["C0FAKE0001"], "source": ["C0FAKE0002"]},
        "retention_days": 30,
        "app": "internal",
        "slack_terms_decision": DECISION,
    }
    return slack(**{**capture, **overrides})


def discord(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "kind": "discord",
        "workspace": DISCORD_WS,
        "channels": {"interface": ["1100000000000000101"]},
        "credentials": {"bot_token": {"env": "BOUNDROOM_KNOWLEDGE_DISCORD_BOT_TOKEN"}},
    }
    entry.update(overrides)
    return entry


def link(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "discord": {"workspace": DISCORD_WS, "user": "1100000000000000201"},
        "slack": {"workspace": SLACK_WS, "user": "U0FAKE0001"},
        "recorded_by": "the operator",
        "recorded_at": "2026-11-12",
    }
    record.update(overrides)
    return record


def write(path: Path, data: Any) -> Path:
    path.write_text(json.dumps(data) if not isinstance(data, str) else data, encoding="utf-8")
    return path


def refusal(model: type[Config], data: Any) -> str:
    with pytest.raises(KnowledgeConfigError) as raised:
        parse(model, data)
    return str(raised.value)


def assert_no_marker(*texts: str) -> None:
    for text in texts:
        for marker in MARKERS:
            assert marker not in text


class RecordingEnviron(MutableMapping[str, str]):
    """An environment that records every variable read, so a test can say which tokens were.

    ``get`` and ``in`` both go through ``__getitem__``, so no read escapes the record.
    """

    def __init__(self, values: Optional[dict[str, str]] = None) -> None:
        self._values = dict(values or {})
        self.read: list[str] = []

    def __getitem__(self, key: str) -> str:
        self.read.append(key)
        return self._values[key]

    def __setitem__(self, key: str, value: str) -> None:
        self._values[key] = value

    def __delitem__(self, key: str) -> None:
        del self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


@pytest.fixture
def process_environ(monkeypatch: pytest.MonkeyPatch) -> RecordingEnviron:
    """The real ``os.environ``, replaced by a recording copy for the test."""
    recording = RecordingEnviron(dict(os.environ))
    monkeypatch.setattr(os, "environ", recording)
    return recording


@pytest.fixture
def files_read(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    read: list[Path] = []
    original = Path.read_text

    def recording(self: Path, *args: Any, **kwargs: Any) -> str:
        read.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", recording)
    return read


class Launches:
    """Adapter factories as RA-KNOW-008 and 009 will write them: look up the entry, read its tokens."""

    def __init__(self, settings: KnowledgeSettings, environ: RecordingEnviron) -> None:
        self.settings = settings
        self.environ = environ
        self.built: list[str] = []

    def factory(self, kind: PlatformKind) -> Callable[[PlatformSelection], ChatPlatform]:
        def build(selection: PlatformSelection) -> ChatPlatform:
            assert selection.kind == kind
            entry = self.settings.platform(selection)
            for ref in entry.secrets().values():
                read_secret(ref, self.environ)
            self.built.append(f"{kind}:{selection.workspace}")
            return FakePlatform(selection)

        return build

    def factories(self) -> dict[str, Callable[[PlatformSelection], ChatPlatform]]:
        return {"discord": self.factory("discord"), "slack": self.factory("slack")}


def token_environ() -> RecordingEnviron:
    return RecordingEnviron(
        {
            "BOUNDROOM_KNOWLEDGE_SLACK_BOT_TOKEN": VALUE_MARKER,
            "BOUNDROOM_KNOWLEDGE_SLACK_APP_TOKEN": VALUE_MARKER,
            "BOUNDROOM_KNOWLEDGE_DISCORD_BOT_TOKEN": VALUE_MARKER,
        }
    )


# CP-1: no platform is enabled by default


def test_cp1_the_operator_file_is_the_one_the_spec_names():
    assert DEFAULT_OPERATOR_FILE == Path("~/.config/boundroom/knowledge.json")


@pytest.mark.parametrize("content", [None, "{}", '{"platforms": []}', '{"platforms": [], "identity_links": []}'])
def test_cp1_an_absent_or_empty_configuration_starts_nothing_and_reads_no_token(tmp_path, content, process_environ):
    path = tmp_path / "knowledge.json"
    if content is not None:
        write(path, content)
    environ = token_environ()

    settings = load(path)
    launches = Launches(settings, environ)

    assert (settings.selections, settings.refusals) == ((), ())
    assert start_platforms(settings.selections, launches.factories()) == ()
    assert launches.built == []
    assert environ.read == []
    assert process_environ.read == []


def test_cp1_no_operator_file_at_all_enables_nothing():
    assert load(None) == KnowledgeSettings()


def test_cp1_loading_a_full_configuration_reads_no_variable_and_no_secret_file(tmp_path, process_environ, files_read):
    secret_file = write(tmp_path / "slack-app-token", VALUE_MARKER)
    operator = write(
        tmp_path / "knowledge.json",
        {
            "platforms": [
                slack_capture(credentials={"bot_token": {"env": "SLACK_BOT"}, "app_token": {"file": str(secret_file)}}),
                discord(),
            ]
        },
    )

    settings = load(operator)

    assert len(settings.selections) == 2
    assert process_environ.read == []
    assert files_read == [operator]


@pytest.mark.parametrize(
    "content", ["{not json", "[]", '{"platforms": {}}', json.dumps({"platforms": [slack(), {"kind": "slack"}]})]
)
def test_cp1_a_refused_file_enables_nothing_and_says_why(tmp_path, content):
    path = write(tmp_path / "knowledge.json", content)

    settings = load(path)

    assert settings.selections == ()
    assert len(settings.refusals) == 1
    assert settings.refusals[0].startswith(f"knowledge: refusing {path}: ")


def test_cp1_a_file_with_one_good_entry_and_one_bad_starts_neither(tmp_path):
    bad = discord(credentials={})
    settings = load(write(tmp_path / "knowledge.json", {"platforms": [slack(), bad]}))

    assert settings.selections == ()
    assert "credentials.bot_token" in settings.refusals[0]


# CP-2: each platform is enabled on its own


@pytest.mark.parametrize("kind", ["discord", "slack"])
def test_cp2_enabling_one_platform_never_starts_or_reads_the_other(tmp_path, kind):
    entry = discord() if kind == "discord" else slack()
    settings = load(write(tmp_path / "knowledge.json", {"platforms": [entry]}))
    environ = token_environ()
    launches = Launches(settings, environ)

    def never(selection: PlatformSelection) -> ChatPlatform:
        raise AssertionError(f"{selection.kind} code ran although only {kind} is enabled")

    factories = launches.factories()
    factories["slack" if kind == "discord" else "discord"] = never
    (started,) = start_platforms(settings.selections, factories)

    assert started.kind == kind
    assert launches.built == [f"{kind}:{entry['workspace']}"]
    assert {name.split("_")[2].lower() for name in environ.read} == {kind}


def test_cp2_both_platforms_start_each_with_its_own_selection_and_tokens(tmp_path):
    settings = load(write(tmp_path / "knowledge.json", {"platforms": [discord(), slack()]}))
    environ = token_environ()
    launches = Launches(settings, environ)

    started = start_platforms(settings.selections, launches.factories())

    assert [(p.kind, p.workspace) for p in started] == [("discord", DISCORD_WS), ("slack", SLACK_WS)]
    assert sorted(environ.read) == sorted(
        [
            "BOUNDROOM_KNOWLEDGE_DISCORD_BOT_TOKEN",
            "BOUNDROOM_KNOWLEDGE_SLACK_APP_TOKEN",
            "BOUNDROOM_KNOWLEDGE_SLACK_BOT_TOKEN",
        ]
    )


def test_cp2_an_entry_is_interface_only_unless_the_operator_adds_source(tmp_path):
    settings = load(write(tmp_path / "knowledge.json", {"platforms": [discord()]}))
    (platform,) = start_platforms(settings.selections, Launches(settings, token_environ()).factories())

    assert platform.roles == frozenset({"interface"})
    with pytest.raises(RoleNotEnabled):
        platform.fetch_since(Place(platform="discord", workspace=DISCORD_WS, channel="1100000000000000101"), None)


def test_cp2_a_selection_carries_exactly_the_configured_roles():
    config = parse(KnowledgeConfig, {"platforms": [discord(roles=["source"], channels={"source": ["1"]}, retention_days=7)]})

    assert config.selections() == (PlatformSelection(kind="discord", workspace=DISCORD_WS, roles=frozenset({"source"})),)


def test_cp2_two_workspaces_on_one_platform_are_two_selections():
    second = slack(workspace="T0FAKE0002")
    config = parse(KnowledgeConfig, {"platforms": [slack(), second]})

    assert [s.workspace for s in config.selections()] == [SLACK_WS, "T0FAKE0002"]


def test_cp2_a_workspace_configured_twice_is_refused_before_start_platforms_sees_it():
    assert "configured twice" in refusal(KnowledgeConfig, {"platforms": [slack(), slack(roles=["interface"])]})


# CP-7: Slack capture waits on the terms decision


def test_cp7_slack_capture_with_the_decision_and_an_internal_app_is_accepted():
    config = parse(KnowledgeConfig, {"platforms": [slack_capture()]})

    (entry,) = config.platforms
    assert entry.selection().roles == frozenset({"interface", "source"})
    assert entry.slack_terms_decision is not None and entry.slack_terms_decision.record == DECISION["record"]


@pytest.mark.parametrize(
    "overrides, reason",
    [
        ({"slack_terms_decision": None}, "waits on the owner's recorded decision on Slack's API terms"),
        ({"app": None}, "needs an internal app"),
        ({"app": "distributed"}, "needs an internal app"),
    ],
    ids=["no decision", "no app declaration", "distributed app"],
)
def test_cp7_slack_capture_without_the_decision_or_an_internal_app_is_refused(overrides, reason):
    entry = slack_capture()
    for key, value in overrides.items():
        if value is None:
            del entry[key]
        else:
            entry[key] = value

    message = refusal(KnowledgeConfig, {"platforms": [entry]})

    assert reason in message
    assert "CP-7" in message


@pytest.mark.parametrize("decision", [{"record": "x"}, {**DECISION, "recorded_at": 1794500000}, {**DECISION, "record": ""}])
def test_cp7_an_incomplete_decision_record_is_refused(decision):
    assert "slack_terms_decision" in refusal(KnowledgeConfig, {"platforms": [slack_capture(slack_terms_decision=decision)]})


def test_cp7_slack_as_an_interface_needs_no_decision():
    (entry,) = parse(KnowledgeConfig, {"platforms": [slack()]}).platforms
    assert entry.roles == frozenset({"interface"})


def test_cp7_discord_capture_needs_no_slack_decision_and_may_not_carry_one():
    parse(KnowledgeConfig, {"platforms": [discord(roles=["source"], channels={"source": ["1"]}, retention_days=7)]})

    assert "only a Slack entry" in refusal(KnowledgeConfig, {"platforms": [discord(app="internal")]})
    assert "only a Slack entry" in refusal(KnowledgeConfig, {"platforms": [discord(slack_terms_decision=DECISION)]})


@pytest.mark.parametrize("field", ["app", "slack_terms_decision"])
def test_cp7_the_repository_can_neither_declare_the_app_nor_record_the_decision(field):
    scope = {"platforms": [{"kind": "slack", "workspace": SLACK_WS, field: DECISION if field != "app" else "internal"}]}

    message = refusal(RepositoryKnowledgeScope, scope)

    assert f"{field}: the repository can't" in message


# CP-11: credentials stay out of the configuration, its errors and its output


@pytest.mark.parametrize(
    "where",
    [
        lambda m: slack(credentials={"bot_token": m, "app_token": {"env": "X"}}),
        lambda m: slack(credentials={"bot_token": {"env": m}, "app_token": {"env": "X"}}),
        lambda m: slack(credentials={"bot_token": {"file": f"/run/secrets/{m}"}, "app_token": {"env": "X"}}),
        lambda m: slack(channels={"interface": [m]}),
        lambda m: slack_capture(slack_terms_decision={**DECISION, "recorded_by": m}),
        lambda m: slack(**{m: "x"}),
    ],
    ids=["credential value", "variable name", "secret file", "channel", "decision record", "key"],
)
@pytest.mark.parametrize("marker", [SLACK_MARKER, APP_MARKER, DISCORD_MARKER, GITHUB_MARKER, f"Bearer {VALUE_MARKER}"])
def test_cp11_a_token_anywhere_in_the_file_is_refused_by_where_it_is_never_what_it_is(tmp_path, capsys, where, marker):
    path = write(tmp_path / "knowledge.json", {"platforms": [where(marker)]})

    settings = load(path)

    assert settings.selections == ()
    assert "looks like a token" in settings.refusals[0]
    captured = capsys.readouterr()
    assert_no_marker(repr(settings), str(settings), *settings.refusals, captured.out, captured.err)


@pytest.mark.parametrize("field", ["token", "bot_token", "app_token", "secret", "password"])
def test_cp11_a_field_that_would_hold_a_token_is_refused_with_its_reason(field):
    message = refusal(KnowledgeConfig, {"platforms": [slack(**{field: "plain-value"})]})

    assert message.startswith(f"platforms.0: {field}: the configuration never holds a token")
    assert "plain-value" not in message


def test_cp11_a_refused_value_never_appears_in_the_error():
    # Not token-shaped, so it passes the scan and reaches pydantic, which must not echo it.
    message = refusal(KnowledgeConfig, {"platforms": [slack(workspace=f"has spaces {VALUE_MARKER}")]})

    assert "platforms.0.workspace" in message
    assert_no_marker(message)


@pytest.mark.parametrize(
    "name",
    ["GITHUB_TOKEN", "GH_TOKEN", "MY_GITHUB_PAT", "GIT_ASKPASS", "KANBAN_TOKEN", "HERMES_API_KEY", "BOUNDROOM_AGENT_RUNNER"],
)
def test_cp11_a_reference_to_an_execution_credential_is_refused(name):
    entry = slack(credentials={"bot_token": {"env": name}, "app_token": {"env": "SLACK_APP"}})

    assert "is not the knowledge service's variable" in refusal(KnowledgeConfig, {"platforms": [entry]})


@pytest.mark.parametrize("name", ["github_token", "kanban_token", "shell", "ssh_key"])
def test_cp11_credentials_hold_only_the_platform_s_own_tokens(name):
    entry = slack(credentials={"bot_token": {"env": "A"}, "app_token": {"env": "B"}, name: {"env": "C"}})

    message = refusal(KnowledgeConfig, {"platforms": [entry]})

    assert f"{name}: the knowledge service holds only its own platform tokens" in message


@pytest.mark.parametrize(
    "ref, reason",
    [
        ({}, "exactly one of env or file"),
        ({"env": "A", "file": "/run/secrets/a"}, "exactly one of env or file"),
        ({"env": "lower_case"}, "env"),
        ({"file": "relative/token"}, "absolute path"),
        ({"file": "/run/secrets/../../home/token"}, "absolute path"),
    ],
)
def test_cp11_a_secret_reference_names_one_variable_or_one_absolute_file(ref, reason):
    entry = discord(credentials={"bot_token": ref})

    assert reason in refusal(KnowledgeConfig, {"platforms": [entry]})


def test_cp11_read_secret_reads_the_named_variable_and_never_shows_the_value(capsys):
    environ = RecordingEnviron({"BOUNDROOM_KNOWLEDGE_SLACK_BOT_TOKEN": VALUE_MARKER})

    secret = read_secret(SecretRef(env="BOUNDROOM_KNOWLEDGE_SLACK_BOT_TOKEN"), environ)

    assert secret.get_secret_value() == VALUE_MARKER
    assert environ.read == ["BOUNDROOM_KNOWLEDGE_SLACK_BOT_TOKEN"]
    print(secret, repr(secret))
    assert_no_marker(str(secret), repr(secret), capsys.readouterr().out)


def test_cp11_read_secret_reads_a_secret_file(tmp_path):
    path = write(tmp_path / "token", f"{VALUE_MARKER}\n")

    secret = read_secret(SecretRef(file=str(path)), {})

    assert secret.get_secret_value() == VALUE_MARKER
    assert_no_marker(repr(secret))


def test_cp11_read_secret_uses_the_process_environment_when_given_none(process_environ):
    process_environ["BOUNDROOM_KNOWLEDGE_TEST_TOKEN"] = VALUE_MARKER

    assert read_secret(SecretRef(env="BOUNDROOM_KNOWLEDGE_TEST_TOKEN")).get_secret_value() == VALUE_MARKER


@pytest.mark.parametrize(
    "ref, named",
    [
        (SecretRef(env="BOUNDROOM_KNOWLEDGE_UNSET"), "environment variable BOUNDROOM_KNOWLEDGE_UNSET"),
        (SecretRef(file="/nonexistent/boundroom/token"), "secret file /nonexistent/boundroom/token"),
    ],
)
def test_cp11_a_missing_secret_names_its_reference_and_nothing_else(ref, named):
    with pytest.raises(SecretUnavailable, match="holds no token") as raised:
        read_secret(ref, {"OTHER": VALUE_MARKER})

    assert named in str(raised.value)
    assert_no_marker(str(raised.value))


def test_cp11_the_loaded_configuration_s_repr_holds_references_only(tmp_path):
    settings = load(write(tmp_path / "knowledge.json", {"platforms": [slack(), discord()]}))

    text = repr(settings)
    assert "BOUNDROOM_KNOWLEDGE_SLACK_BOT_TOKEN" in text
    assert_no_marker(text)


# Refused fields: every one rejected with a named reason


@pytest.mark.parametrize("field", ["run_next", "shell", "supervisor", "queue", "kanban", "github", "github_token", "sync"])
def test_a_top_level_field_across_the_trust_boundary_is_refused(field):
    message = refusal(KnowledgeConfig, {"platforms": [], field: {}})

    assert message.startswith(f"{field}: the knowledge service has no path to run_next")


@pytest.mark.parametrize("field", ["endpoint", "url", "webhook", "webhook_url"])
def test_a_service_address_is_refused(field):
    assert f"{field}: adapters connect outbound" in refusal(KnowledgeConfig, {"platforms": [discord(**{field: "x"})]})


@pytest.mark.parametrize(
    "entry, reason",
    [
        (discord(channels={}), "channels.interface: the interface role on discord workspace"),
        (discord(channels={"interface": ["1"], "source": ["2"]}), "channels.source: the source role is not enabled"),
        (discord(roles=[]), "roles"),
        (discord(roles=["admin"]), "roles"),
        (discord(roles=["source"], channels={"source": ["2"]}), "retention_days: the source role on"),
        (discord(retention_days=7), "retention_days: the source role is not enabled"),
        (discord(retention_days=True, roles=["source"], channels={"source": ["2"]}), "retention_days"),
        (discord(retention_days="7", roles=["source"], channels={"source": ["2"]}), "retention_days"),
        (
            discord(roles=["source"], channels={"source": ["2"]}, retention_days=7, answer_audiences=["thread"]),
            "answer_audiences",
        ),
        (discord(roles=["source"], channels={"source": ["2"]}, retention_days=7, share_destinations=[{"channel": "3"}]), "share"),
        (discord(answer_audiences=["public"]), "answer_audiences"),
        (discord(answer_audiences=[]), "answer_audiences"),
        (discord(credentials={}), "credentials.bot_token: discord workspace"),
        (discord(credentials={"bot_token": {"env": "A"}, "app_token": {"env": "B"}}), "credentials.app_token: discord doesn't"),
        (slack(credentials={"bot_token": {"env": "A"}}), "credentials.app_token: slack workspace"),
        (discord(workspace=None), "workspace"),
        ({k: v for k, v in discord().items() if k != "credentials"}, "credentials"),
        (discord(kind="teams"), "kind"),
        (discord(colour="blue"), "colour"),
    ],
)
def test_each_refused_entry_names_its_field_and_reason(entry, reason):
    assert reason in refusal(KnowledgeConfig, {"platforms": [entry]})


def test_answer_audiences_default_to_the_asker_only():
    (entry,) = parse(KnowledgeConfig, {"platforms": [discord()]}).platforms
    assert entry.answer_audiences == frozenset({"ephemeral"})
    assert entry.share_destinations == frozenset()


def test_share_destinations_become_places_on_the_entry_s_own_workspace():
    (entry,) = parse(
        KnowledgeConfig, {"platforms": [slack(share_destinations=[{"channel": "C0FAKE0009", "thread": "1700000000.0001"}])]}
    ).platforms

    (share,) = entry.share_destinations
    assert entry.destination(share) == Place(platform="slack", workspace=SLACK_WS, channel="C0FAKE0009", thread="1700000000.0001")


def test_settings_refuse_a_selection_that_is_not_configured():
    with pytest.raises(KnowledgeConfigError, match="not configured"):
        KnowledgeSettings().platform(PlatformSelection(kind="slack", workspace=SLACK_WS, roles=frozenset({"interface"})))


# Identity links: explicit, operator-maintained records


def test_an_identity_link_names_one_user_on_each_platform():
    config = parse(KnowledgeConfig, {"platforms": [discord(), slack()], "identity_links": [link()]})

    (record,) = config.identity_links
    assert record.principals() == (
        Principal(platform="discord", workspace=DISCORD_WS, user="1100000000000000201"),
        Principal(platform="slack", workspace=SLACK_WS, user="U0FAKE0001"),
    )


@pytest.mark.parametrize(
    "links, reason",
    [
        ([link(slack={"workspace": "T0OTHER", "user": "U1"})], "identity_links.0: slack workspace T0OTHER is not configured"),
        ([link(), link()], "a link is recorded twice"),
        ([link(slack={"workspace": SLACK_WS, "user": "someone@example.com"})], "identity_links.0.slack.user"),
        ([link(slack={"workspace": SLACK_WS, "user": "Jane Doe"})], "identity_links.0.slack.user"),
        ([{k: v for k, v in link().items() if k != "recorded_by"}], "recorded_by"),
        ([link(match="email")], "match"),
    ],
    ids=["unconfigured workspace", "duplicate", "email", "display name", "unrecorded", "inferred"],
)
def test_an_identity_link_that_is_not_an_explicit_record_is_refused(links, reason):
    assert reason in refusal(KnowledgeConfig, {"platforms": [discord(), slack()], "identity_links": links})


# The repository's scope: narrow-only


def operator(*entries: dict[str, Any], links: Optional[list[dict[str, Any]]] = None) -> KnowledgeConfig:
    return parse(KnowledgeConfig, {"platforms": list(entries), "identity_links": links or []})


def scope(*entries: dict[str, Any]) -> RepositoryKnowledgeScope:
    return parse(RepositoryKnowledgeScope, {"platforms": list(entries)})


def test_the_repository_can_turn_a_platform_off_and_its_links_go_with_it():
    narrowed = narrow(
        operator(discord(), slack(), links=[link()]), scope({"kind": "slack", "workspace": SLACK_WS, "enabled": False})
    )

    assert [s.kind for s in narrowed.selections()] == ["discord"]
    assert narrowed.identity_links == ()


def test_the_repository_can_narrow_roles_channels_audiences_shares_and_retention():
    entry = slack_capture(
        channels={"interface": ["C0FAKE0001", "C0FAKE0003"], "source": ["C0FAKE0002", "C0FAKE0004"]},
        answer_audiences=["ephemeral", "thread"],
        share_destinations=[{"channel": "C0FAKE0005"}, {"channel": "C0FAKE0006"}],
    )
    asked = {
        "kind": "slack",
        "workspace": SLACK_WS,
        "channels": {"interface": ["C0FAKE0003"], "source": ["C0FAKE0004"]},
        "answer_audiences": ["ephemeral"],
        "share_destinations": [{"channel": "C0FAKE0006"}],
        "retention_days": 7,
    }

    (narrowed,) = narrow(operator(entry), scope(asked)).platforms

    assert narrowed.channels.interface == frozenset({"C0FAKE0003"})
    assert narrowed.channels.source == frozenset({"C0FAKE0004"})
    assert narrowed.answer_audiences == frozenset({"ephemeral"})
    assert [d.channel for d in narrowed.share_destinations] == ["C0FAKE0006"]
    assert narrowed.retention_days == 7
    assert narrowed.credentials == parse(KnowledgeConfig, {"platforms": [entry]}).platforms[0].credentials


def test_dropping_a_role_drops_what_belonged_to_it():
    asked = {"kind": "slack", "workspace": SLACK_WS, "roles": ["interface"]}

    (narrowed,) = narrow(operator(slack_capture()), scope(asked)).platforms

    assert narrowed.selection().roles == frozenset({"interface"})
    assert (narrowed.channels.source, narrowed.retention_days) == (frozenset(), None)


def test_dropping_the_interface_role_drops_its_audiences_and_shares():
    entry = slack_capture(answer_audiences=["thread"], share_destinations=[{"channel": "C0FAKE0005"}])

    (narrowed,) = narrow(operator(entry), scope({"kind": "slack", "workspace": SLACK_WS, "roles": ["source"]})).platforms

    assert narrowed.roles == frozenset({"source"})
    assert (narrowed.answer_audiences, narrowed.share_destinations) == (frozenset({"ephemeral"}), frozenset())


def test_a_platform_the_repository_does_not_mention_is_unchanged():
    config = operator(discord(), slack())

    assert narrow(config, scope({"kind": "slack", "workspace": SLACK_WS, "enabled": False})).platforms == (config.platforms[0],)
    assert narrow(config, scope()) == config


@pytest.mark.parametrize(
    "asked, reason",
    [
        ({"kind": "slack", "workspace": "T0FAKE0002"}, "the repository can't add a platform or a workspace"),
        ({"kind": "discord", "workspace": SLACK_WS}, "the repository can't add a platform or a workspace"),
        ({"kind": "slack", "workspace": SLACK_WS, "roles": ["interface", "source"]}, "roles on slack workspace"),
        ({"kind": "slack", "workspace": SLACK_WS, "channels": {"interface": ["C0NEW"]}}, "can't add C0NEW"),
        ({"kind": "slack", "workspace": SLACK_WS, "answer_audiences": ["thread"]}, "can't add thread"),
        ({"kind": "slack", "workspace": SLACK_WS, "share_destinations": [{"channel": "C0NEW"}]}, "destination C0NEW"),
        ({"kind": "slack", "workspace": SLACK_WS, "retention_days": 7}, "can only shorten retention"),
        ({"kind": "slack", "workspace": SLACK_WS, "channels": {"interface": []}}, "needs at least one allowlisted channel"),
    ],
    ids=["workspace", "platform", "role", "channel", "audience", "destination", "retention", "empties a role"],
)
def test_the_repository_can_not_widen_anything(asked, reason):
    with pytest.raises(KnowledgeConfigError, match=reason):
        narrow(operator(slack()), scope(asked))


def test_the_repository_can_not_lengthen_retention():
    with pytest.raises(KnowledgeConfigError, match="can only shorten retention"):
        narrow(operator(slack_capture()), scope({"kind": "slack", "workspace": SLACK_WS, "retention_days": 31}))


def test_the_repository_can_not_scope_one_platform_twice():
    asked = {"kind": "slack", "workspace": SLACK_WS}
    with pytest.raises(KnowledgeConfigError, match="scoped twice"):
        narrow(operator(slack()), scope(asked, asked))


@pytest.mark.parametrize(
    "data, reason",
    [
        ({"platforms": [{"kind": "slack", "workspace": SLACK_WS, "enabled": True}]}, "can only turn a platform off"),
        ({"platforms": [{"kind": "slack", "workspace": SLACK_WS, "enabled": 1}]}, "can only turn a platform off"),
        (
            {"platforms": [{"kind": "slack", "workspace": SLACK_WS, "credentials": {"bot_token": {"env": "X"}}}]},
            "credentials: the repository can't hold or name a credential",
        ),
        ({"platforms": [{"kind": "slack", "workspace": SLACK_WS, "bot_token": "x"}]}, "never holds a token"),
        ({"platforms": [], "identity_links": []}, "identity_links: identity links are operator-maintained"),
        ({"platforms": [], "run_next": True}, "run_next: the knowledge service has no path"),
    ],
)
def test_a_repository_scope_field_that_widens_is_refused_with_its_reason(data, reason):
    assert reason in refusal(RepositoryKnowledgeScope, data)


def test_load_applies_the_repository_scope(tmp_path):
    operator_file = write(tmp_path / "knowledge.json", {"platforms": [discord(), slack()]})
    repository_file = write(
        tmp_path / "repo-knowledge.json", {"platforms": [{"kind": "discord", "workspace": DISCORD_WS, "enabled": False}]}
    )

    settings = load(operator_file, repository_file)

    assert [s.kind for s in settings.selections] == ["slack"]
    assert settings.refusals == ()


def test_load_ignores_a_repository_scope_that_is_absent(tmp_path):
    operator_file = write(tmp_path / "knowledge.json", {"platforms": [discord()]})

    assert len(load(operator_file, tmp_path / "absent.json").selections) == 1


@pytest.mark.parametrize(
    "repository",
    [
        {"platforms": [{"kind": "slack", "workspace": "T0FAKE0002"}]},
        {"platforms": [{"kind": "discord", "workspace": DISCORD_WS, "credentials": {}}]},
        "{not json",
    ],
    ids=["adds a workspace", "names a credential", "unreadable"],
)
def test_a_refused_repository_scope_starts_nothing_rather_than_run_wider(tmp_path, repository):
    operator_file = write(tmp_path / "knowledge.json", {"platforms": [discord(), slack()]})
    repository_file = write(tmp_path / "repo-knowledge.json", repository)

    settings = load(operator_file, repository_file)

    assert settings.selections == ()
    assert settings.refusals[0].startswith(f"knowledge: refusing {repository_file}, so no platform starts: ")


def test_a_token_in_the_repository_scope_is_refused_without_showing_it(tmp_path):
    operator_file = write(tmp_path / "knowledge.json", {"platforms": [slack()]})
    repository_file = write(
        tmp_path / "repo.json", {"platforms": [{"kind": "slack", "workspace": SLACK_WS, "note": SLACK_MARKER}]}
    )

    settings = load(operator_file, repository_file)

    assert "looks like a token" in settings.refusals[0]
    assert_no_marker(repr(settings), *settings.refusals)
