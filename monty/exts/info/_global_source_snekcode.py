"""
Do not import this file.

NOTE: THIS RUNS ON PYTHON 3.11
"""

# exit codes:
# 0: success
# 1: indeterminate error
# 2: module not resolvable
# 3: attribute does not exist
# 4: invalid characters, not a valid object path
# 5: dynamically created object
# 6: is a builtin object, prints module name
# 7: invalid metadata
# 8: unsupported package (does not use github)
# 9: module found but cannot find class definition

if __name__ == "__main__":
    import importlib
    import importlib.metadata
    import importlib.util
    import inspect
    import pathlib
    import pkgutil
    import sys
    import tracemalloc
    import types
    from typing import Any

    # establish the object itself
    object_name = """REPLACE_THIS_STRING_WITH_THE_OBJECT_NAME"""

    tracemalloc.start()
    try:
        src: Any = pkgutil.resolve_name(object_name)
    except ModuleNotFoundError:
        sys.exit(2)
    except AttributeError:
        sys.exit(3)
    except ValueError:
        sys.exit(4)
    except Exception:
        raise

    try:
        unwrapped = inspect.unwrap(src)
        if isinstance(unwrapped, property) and unwrapped.fget:
            unwrapped = inspect.unwrap(unwrapped.fget)
    except Exception:
        # continue with possibly wrapped src object in case of error
        pass
    else:
        src = unwrapped

    trace = tracemalloc.get_object_traceback(src)
    tracemalloc.stop()

    # get the source of the object

    try:
        filename = inspect.getsourcefile(src)
    except TypeError:
        if isinstance(src, types.BuiltinFunctionType):
            sys.exit(6)
        # if we have to use tracemalloc we have a bit of a problem
        # the code is dynamically created
        # this means that we need to establish the file, where the def starts, and where it ends
        if not trace:
            sys.exit(5)
        frame = trace[-1]
        filename = frame.filename
        first_lineno = frame.lineno
        lines_extension = f"#L{frame.lineno}"
        parents: list[str] = []
        try:
            name: str = src.__qualname__
        except AttributeError:
            name = object_name.rsplit(".", 1)[-1]
        else:
            if "." in name:
                parents_str, name = name.rsplit(".", 1)
                parents = parents_str.split(".")
        try:
            with open(filename) as f:
                sourcecode = f.read()
        except FileNotFoundError:
            sys.exit(5)

        # Once we know where the definition starts, we can hopefully use ast for parsing the file
        import ast

        parsed = ast.parse(sourcecode, filename=filename)
        _endlines: set[tuple[int, int]] = set()
        for node in ast.walk(parsed):
            if not hasattr(node, "lineno"):
                continue
            if node.lineno < first_lineno:
                continue
            if isinstance(node, ast.Assign):
                target = node.targets[0]
            elif isinstance(node, ast.AnnAssign):
                target = node.target
            else:
                continue
            if parents:
                if getattr(target, "attr", None) != name:
                    continue
            elif getattr(target, "id", None) != name:
                continue
            if node.end_lineno:
                end_lineno = node.end_lineno
            else:
                end_lineno = node.lineno
            _endlines.add((node.lineno, end_lineno))

        if _endlines:
            lineno, end_lineno = sorted(_endlines, key=lambda i: i[0])[0]
            lines_extension = f"#L{lineno}"
            if end_lineno > lineno:
                lines_extension += f"-L{end_lineno}"

        module_name = object_name.split(":", 1)[0] if ":" in object_name else object_name.rsplit(".", 1)[0]
    else:
        if not inspect.ismodule(src):
            try:
                lines, first_lineno = inspect.getsourcelines(src)
            except OSError:
                print(filename)
                sys.exit(9)
            lines_extension = f"#L{first_lineno}-L{first_lineno+len(lines)-1}"
        else:
            lines_extension = ""
        module_name = ""
    if not filename:
        sys.exit(6)

    if not module_name:
        module = inspect.getmodule(src)
        if not module:
            sys.exit(4)
        module_name = module.__name__
    top_module_name = module_name.split(".", 1)[0]

    # determine the actual file name
    try:
        file = inspect.getsourcefile(importlib.import_module(top_module_name))
        if file is None:
            raise ValueError
        filename = str(pathlib.Path(filename).relative_to(pathlib.Path(file).parent.parent))
        filename = filename.removeprefix("site-packages/")
    except ValueError:
        sys.exit(5)

    # get the version and link to the source of the module
    if top_module_name in sys.stdlib_module_names:  # type: ignore # this code runs on py3.10
        if top_module_name in sys.builtin_module_names:
            sys.exit(6)
        # handle the object being part of the stdlib
        import platform

        python_version = f"python{platform.python_version().rsplit('.', 1)[0]}/"
        if filename.startswith(python_version):
            filename = filename.split("/", 1)[-1]
        url = f"https://github.com/python/cpython/blob/v{platform.python_version()}/Lib/{filename}{lines_extension}"
    else:
        # assume that the source is github
        try:
            metadata = importlib.metadata.metadata(top_module_name)
        except importlib.metadata.PackageNotFoundError:
            print(f"Sorry, I can't find the metadata for `{object_name}`.")
            sys.exit(7)
        # print(metadata.keys())
        version = metadata["Version"]
        for url in [metadata.get("Home-page"), *metadata.json.get("project_url", [])]:  # type: ignore # runs on py3.10
            if not url:
                continue
            url = url.split(",", 1)[-1].strip().rstrip("/")
            # there are 4 `/` in a github link
            if url.startswith(("https://github.com/", "http://github.com/")) and url.count("/") == 4:
                break
        else:
            print("This package isn't supported right now.")
            sys.exit(8)
        # I ideally want to use the database for this and run that locally by sending a pickled result.
        src_dir = ""
        if top_module_name == "sqlalchemy":
            version = f"rel_{version}".replace(".", "_")
            src_dir = "lib/"
        elif top_module_name not in ("arrow", "databases", "ormar", "typing_extensions"):
            version = f"v{version}"

        url += f"/blob/{version}/{src_dir}{filename}{lines_extension}"
    # used to be able to slice code to ignore import side-effects
    print("#" * 80)
    print(url)
