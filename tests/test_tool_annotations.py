"""R1 regression tests: MCP tool annotations + transport wiring.

Every listed tool now carries ToolAnnotations derived from its
side_effect_class. Annotations are untrusted *hints* per the MCP spec —
these tests pin the mapping semantics so a future refactor can't
accidentally advertise a send tool as read-only.
"""
from __future__ import annotations

from src.main import TOOL_ANNOTATIONS


def test_annotation_map_covers_all_side_effect_classes():
    assert set(TOOL_ANNOTATIONS) == {"read", "write", "destructive", "send"}


def test_read_annotations_are_read_only_idempotent_closed_world():
    a = TOOL_ANNOTATIONS["read"]
    assert a.readOnlyHint is True
    assert a.destructiveHint is False
    assert a.idempotentHint is True
    assert a.openWorldHint is False


def test_write_annotations_not_destructive():
    a = TOOL_ANNOTATIONS["write"]
    assert a.readOnlyHint is False
    assert a.destructiveHint is False


def test_destructive_and_send_marked_destructive():
    assert TOOL_ANNOTATIONS["destructive"].destructiveHint is True
    assert TOOL_ANNOTATIONS["send"].destructiveHint is True


def test_send_is_open_world():
    # Sending leaves the mailbox trust boundary toward arbitrary
    # external recipients — the one true open-world class here.
    assert TOOL_ANNOTATIONS["send"].openWorldHint is True
    assert TOOL_ANNOTATIONS["destructive"].openWorldHint is False


def test_no_class_advertises_read_only_except_read():
    for cls, a in TOOL_ANNOTATIONS.items():
        if cls != "read":
            assert a.readOnlyHint is False, cls


def test_streamable_http_manager_importable_and_stateless_capable():
    # Pin the SDK surface main.py relies on (mcp>=1.27,<2).
    import inspect

    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    params = inspect.signature(StreamableHTTPSessionManager.__init__).parameters
    assert "stateless" in params
    assert "json_response" in params
