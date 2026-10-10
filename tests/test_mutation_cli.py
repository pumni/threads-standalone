from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from threads_platform.standalone.__main__ import main
from threads_platform.standalone.accounts import LocalAccountStore
from threads_platform.standalone.mutations import LocalOperationStore


def test_mutation_cli_contract_and_operation_show_is_local_only(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    import threads_platform.standalone.__main__ as cli

    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(tmp_path))
    accounts = LocalAccountStore(tmp_path)
    account = accounts.add("synthetic_account")
    operation = LocalOperationStore(tmp_path).create_received(account.id)

    def unexpected_app(*_args, **_kwargs):
        raise AssertionError("operation show must not create an API runtime")

    monkeypatch.setattr(cli, "build_standalone_app", unexpected_app)
    assert main(["operation", "show", str(operation.id)]) == 0
    output = capsys.readouterr().out
    assert f"operation {operation.id} kind=POST_TEXT phase=RECEIVED" in output
    assert "container=- media=- outcome=-" in output

    parser = cli._build_parser()
    assert parser.parse_args(["post", "synthetic_account", "synthetic text"]).text == (
        "synthetic text"
    )
    assert (
        parser.parse_args(
            [
                "reply",
                "synthetic_account",
                "synthetic_thread",
                "synthetic reply",
                "--parent-reply-id",
                "synthetic_parent",
            ]
        ).parent_reply_id
        == "synthetic_parent"
    )
    assert (
        parser.parse_args(
            [
                "post-image",
                "synthetic_account",
                "https://example.invalid/image",
                "--alt-text",
                "synthetic alt",
            ]
        ).alt_text
        == "synthetic alt"
    )
    assert (
        parser.parse_args(
            ["post-video", "synthetic_account", "https://example.invalid/video"]
        ).video_url
        == "https://example.invalid/video"
    )
    assert (
        parser.parse_args(["post-carousel", "synthetic_account", "manifest.json"]).manifest
        == "manifest.json"
    )
    assert (
        parser.parse_args(["moderate-reply", "synthetic_account", "synthetic_reply", "hide"]).action
        == "hide"
    )


def test_mutation_cli_dispatch_uses_only_injected_synthetic_runtime(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    import threads_platform.standalone.__main__ as cli

    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(tmp_path))
    LocalAccountStore(tmp_path).add("synthetic_account")

    class FakeMutations:
        async def publish_text(self, alias: str, text: str):
            assert (alias, text) == ("synthetic_account", "synthetic text")
            return SimpleNamespace(operation_id="synthetic_operation", media_id="synthetic_media")

        async def create_reply(
            self, alias: str, thread_id: str, text: str, *, parent_reply_id: str | None
        ):
            assert (alias, thread_id, text, parent_reply_id) == (
                "synthetic_account",
                "synthetic_thread",
                "synthetic reply",
                "synthetic_parent",
            )
            return SimpleNamespace(operation_id="synthetic_operation", reply_id="synthetic_reply")

        async def publish_image(self, alias: str, image_url: str, text: str | None, *, alt_text):
            assert (alias, image_url, text, alt_text) == (
                "synthetic_account",
                "https://example.invalid/image",
                "synthetic caption",
                "synthetic alt",
            )
            return SimpleNamespace(operation_id="synthetic_operation", media_id="synthetic_media")

        async def publish_video(self, alias: str, video_url: str, text: str | None, *, alt_text):
            assert (alias, video_url, text, alt_text) == (
                "synthetic_account",
                "https://example.invalid/video",
                None,
                None,
            )
            return SimpleNamespace(operation_id="synthetic_operation", media_id="synthetic_media")

        async def publish_carousel(self, alias: str, manifest):
            assert alias == "synthetic_account"
            assert len(manifest.items) == 2
            return SimpleNamespace(operation_id="synthetic_operation", media_id="synthetic_media")

        async def moderate_reply(self, alias: str, reply_id: str, action: str):
            assert (alias, reply_id, action) == (
                "synthetic_account",
                "synthetic_reply",
                "hide",
            )
            return SimpleNamespace(operation_id="synthetic_operation", action=action)

    @asynccontextmanager
    async def fake_app(root: Path, accounts=None):
        del root, accounts
        yield SimpleNamespace(mutations=FakeMutations())

    monkeypatch.setattr(cli, "build_standalone_app", fake_app)
    manifest_path = tmp_path / "carousel.json"
    manifest_path.write_text(
        json.dumps(
            {
                "items": [
                    {"media_type": "IMAGE", "url": "https://example.invalid/one"},
                    {"media_type": "IMAGE", "url": "https://example.invalid/two"},
                ]
            }
        ),
        encoding="utf-8",
    )

    invocations = [
        (["post", "synthetic_account", "synthetic text"], "published operation="),
        (
            [
                "reply",
                "synthetic_account",
                "synthetic_thread",
                "synthetic reply",
                "--parent-reply-id",
                "synthetic_parent",
            ],
            "replied operation=",
        ),
        (
            [
                "post-image",
                "synthetic_account",
                "https://example.invalid/image",
                "synthetic caption",
                "--alt-text",
                "synthetic alt",
            ],
            "published-image operation=",
        ),
        (
            ["post-video", "synthetic_account", "https://example.invalid/video"],
            "published-video operation=",
        ),
        (
            ["post-carousel", "synthetic_account", str(manifest_path)],
            "published-carousel operation=",
        ),
        (
            ["moderate-reply", "synthetic_account", "synthetic_reply", "hide"],
            "moderated operation=",
        ),
    ]
    for arguments, expected in invocations:
        assert main(arguments) == 0
        assert expected in capsys.readouterr().out
