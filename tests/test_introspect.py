"""introspect 边界测试：类型映射（含 Optional / Union / X|None）、可变参数跳过、
非可调用拒绝、模块/实例发现、无 docstring 回退、摘要取首段。

注意：本文件**不使用** ``from __future__ import annotations``，否则函数注解会变成
字符串（PEP 563），而 introspect 当前只内省真实类型对象、不 eval 字符串注解。
"""

import inspect
import types
from typing import Optional, Union

import pytest

from yai_core.discovery.introspect import (
    _annotation_to_json_type,
    build_spec,
    discover,
)


def test_annotation_empty_falls_back_string() -> None:
    assert _annotation_to_json_type(inspect.Parameter.empty) == ("string", False)


def test_annotation_known_and_unknown_types() -> None:
    assert _annotation_to_json_type(int) == ("integer", False)
    assert _annotation_to_json_type(str) == ("string", False)
    # 无法识别的注解回退 string。
    assert _annotation_to_json_type(object) == ("string", False)


def test_annotation_typing_optional_nullable() -> None:
    assert _annotation_to_json_type(Optional[int]) == ("integer", True)  # noqa: UP045


def test_annotation_pipe_union_nullable() -> None:
    # Python 3.10+ 形式 X | None（types.UnionType）。
    assert _annotation_to_json_type(str | None) == ("string", True)


def test_annotation_plain_union_first_member() -> None:
    # 非可空 Union：取第一个成员，nullable=False。
    assert _annotation_to_json_type(Union[int, str]) == ("integer", False)  # noqa: UP007


def test_build_spec_rejects_non_callable() -> None:
    with pytest.raises(TypeError, match="可调用"):
        build_spec(123)  # type: ignore[arg-type]


def test_build_spec_skips_var_args() -> None:
    def variadic(*args, **kwargs):  # noqa: ANN002, ANN003
        """文档。"""

    props = build_spec(variadic).input_schema["properties"]
    assert "args" not in props and "kwargs" not in props


def test_build_spec_nullable_param_type_array() -> None:
    def fn(age: int | None = None) -> None:
        """doc。"""

    prop = build_spec(fn).input_schema["properties"]["age"]
    assert prop["type"] == ["integer", "null"]


def test_build_spec_no_docstring_falls_back_name() -> None:
    def fn(x: int) -> None: ...

    spec = build_spec(fn, name="custom_name")
    assert spec.description == "custom_name"


def test_build_spec_docstring_first_paragraph() -> None:
    def fn() -> None:
        """第一段摘要。

        后面更多内容不应进入 description。
        """

    assert build_spec(fn).description == "第一段摘要。"


class _SampleHost:
    def public_tool(self) -> str:
        """公开方法。"""
        return "ok"

    def _private(self) -> None:
        """下划线方法应被排除。"""


def test_discover_instance_methods_excludes_private() -> None:
    names = [s.name for s in discover(_SampleHost())]
    assert "public_tool" in names and "_private" not in names


def test_discover_module_only_owns_functions() -> None:
    mod = types.ModuleType("fake_mod")

    def owned_fn() -> None:
        """本模块函数。"""

    def _hidden() -> None:
        """下划线函数。"""

    def imported_fn() -> None:
        """外部模块函数（__module__ 不同）。"""

    owned_fn.__module__ = "fake_mod"
    _hidden.__module__ = "fake_mod"
    imported_fn.__module__ = "some_other_mod"
    mod.owned_fn = owned_fn
    mod._hidden = _hidden
    mod.imported_fn = imported_fn

    assert [s.name for s in discover(mod)] == ["owned_fn"]
