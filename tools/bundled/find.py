import fnmatch
import re


async def run(tool, query: str, directory: str = "work", filenames: bool = False, limit: int = 100):
    """Find local files or matching text lines in your accessible work, memory, inbox or history. Use a glob query for filenames, otherwise a regular expression. Results include exact paths and line numbers."""
    if limit < 1 or limit > 1000:
        raise ValueError("Choose 1 to 1000 matches")
    root = tool.path(directory)
    expression = None if filenames else re.compile(query, re.IGNORECASE)
    matches = []
    truncated = False
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if filenames:
            if fnmatch.fnmatch(path.name, query):
                matches.append({"path": str(path)})
        else:
            try:
                text = path.read_bytes().decode()
            except (UnicodeError, OSError):
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if expression.search(line):
                    matches.append(
                        {
                            "path": str(path),
                            "line": number,
                            "text": line[:2000],
                            "line_truncated": len(line) > 2000,
                        }
                    )
                    if len(matches) >= limit:
                        break
        if len(matches) >= limit:
            truncated = True
            break
    return {"matches": matches, "more_may_exist": truncated}
