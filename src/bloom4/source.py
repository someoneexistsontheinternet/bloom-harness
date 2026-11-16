"""Python source navigation without importing or executing the inspected module."""

import ast
import hashlib


def outline(text):
    """Describe modules, classes and functions, including decorators and top-level effects."""
    tree = ast.parse(text)
    symbols = []

    def visit(nodes, prefix=""):
        for node in nodes:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                name = prefix + node.name
                signature = ("class " + node.name + "(" + ", ".join(ast.unparse(x) for x in node.bases) + ")"
                             if isinstance(node, ast.ClassDef) else
                             ("async " if isinstance(node, ast.AsyncFunctionDef) else "") +
                             "def " + node.name + "(" + ast.unparse(node.args) + ")" +
                             (" -> " + ast.unparse(node.returns) if node.returns else ""))
                symbols.append({"name": name, "signature": signature,
                    "description": ast.get_docstring(node) or "No description; retrieve source to understand behavior.",
                    "start": min([node.lineno, *[d.lineno for d in node.decorator_list]]),
                    "end": node.end_lineno,
                    "decorators": [ast.unparse(d) for d in node.decorator_list]})
                visit(node.body, name + ".")

    visit(tree.body)
    top_level = [{"kind": type(n).__name__, "start": n.lineno, "end": n.end_lineno}
                 for n in tree.body if not isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))]
    return {"sha256": hashlib.sha256(text.encode()).hexdigest(),
            "module_description": ast.get_docstring(tree), "symbols": symbols,
            "top_level": top_level,
            "notice": "Descriptions are navigation aids; imports and top-level statements can have effects."}
