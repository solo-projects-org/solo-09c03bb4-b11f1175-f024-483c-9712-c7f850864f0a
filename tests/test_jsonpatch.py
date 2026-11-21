import pytest

from app.jsonpatch import PatchError, apply_patch


def test_add_remove_replace():
    document = {"a": {"b": [1, 3]}, "c": 1}
    patch = [
        {"op": "replace", "path": "/c", "value": 2},
        {"op": "add", "path": "/a/b/1", "value": 2},
        {"op": "add", "path": "/a/b/-", "value": 4},
        {"op": "remove", "path": "/a/b/0"},
        {"op": "add", "path": "/a/d", "value": {"x": 1}},
    ]
    result = apply_patch(document, patch)
    assert result == {"a": {"b": [2, 3, 4], "d": {"x": 1}}, "c": 2}
    # The input is untouched.
    assert document == {"a": {"b": [1, 3]}, "c": 1}


def test_move_copy_test():
    document = {"a": {"x": 1}, "b": [1, 2]}
    patch = [
        {"op": "test", "path": "/a", "value": {"x": 1}},
        {"op": "copy", "from": "/a/x", "path": "/a/y"},
        {"op": "move", "from": "/b/0", "path": "/a/x"},
    ]
    assert apply_patch(document, patch) == {"a": {"x": 1, "y": 1}, "b": [2]}


def test_pointer_escapes():
    document = {"a/b": {"m~n": 1}}
    assert apply_patch(document, [{"op": "replace", "path": "/a~1b/m~0n", "value": 2}]) == {
        "a/b": {"m~n": 2}
    }


def test_failed_test_operation_aborts_without_mutation():
    document = {"a": 1}
    with pytest.raises(PatchError):
        apply_patch(document, [{"op": "replace", "path": "/a", "value": 2}, {"op": "test", "path": "/a", "value": 3}])
    assert document == {"a": 1}


def test_test_operation_uses_numeric_equality():
    assert apply_patch({"a": 1}, [{"op": "test", "path": "/a", "value": 1.0}])


@pytest.mark.parametrize(
    "patch",
    [
        [{"op": "add", "path": "/x"}] ,  # missing value
        [{"op": "remove", "path": "/missing"}],
        [{"op": "replace", "path": "/missing", "value": 1}],
        [{"op": "add", "path": "x", "value": 1}],  # not a pointer
        [{"op": "add", "path": "/~2", "value": 1}],  # bad escape
        [{"op": "add", "path": "/a/2", "value": 1}],  # index out of range
        [{"op": "add", "path": "/a/01", "value": 1}],  # leading zero
        [{"op": "add", "path": "/a/x", "value": 1}],  # into a scalar
        [{"op": "move", "from": "/a/0", "path": "/a/0/x"}],
        "not a list",
        [42],
    ],
)
def test_invalid_patches_raise(patch):
    with pytest.raises(PatchError):
        apply_patch({"a": [1]}, patch)


def test_remove_root_is_rejected():
    with pytest.raises(PatchError):
        apply_patch({"a": 1}, [{"op": "remove", "path": ""}])
