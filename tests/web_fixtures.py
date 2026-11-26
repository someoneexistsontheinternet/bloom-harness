"""Public v3 Safari regression fixtures retained for v4.1 parity."""


def fixture_pages():
    comments = "".join(
        f'<tr class="comtr" id="{700000 + i}"><td><table><tr><td>'
        f'<a class="hnuser" href="https://news.ycombinator.com/user?id=reader">reader</a> '
        f'<span class="age"><a href="https://news.ycombinator.com/item?id={700000 + i}">1 day ago</a></span>'
        f"<p>bloom-comment-{i:04d}: complete substantive discussion with distinct evidence preserved.</p>"
        '<a href="https://sources.example/report">Original report</a> '
        f'<a href="https://news.ycombinator.com/reply?id={700000 + i}">reply</a>'
        "</td></tr></table></td></tr>"
        for i in range(483)
    )
    return {
        "/discussion": '<table role="presentation"><tr><td><table>'
        + comments
        + "</table></td></tr></table>",
        "/tables": """<main><p>Before the complete results.</p>
<table><caption>Unique results caption</caption><thead>
<tr><th rowspan="2">Region</th><th colspan="2">Annual results</th></tr>
<tr><th>2025</th><th>2026</th></tr></thead><tbody>
<tr><th scope="row">North</th><td>17</td><td>29</td></tr>
<tr><th scope="row">South</th><td>31</td><td>43</td></tr></tbody></table>
<table><tr><td>alpha</td><td>71</td></tr><tr><td>beta</td><td>83</td></tr></table>
<p>After the complete results.</p></main>""",
        "/links": """<main><p>Use the <a href="https://sources.example/report">primary report</a>.</p>
<p>Compare its <a href="https://sources.example/report">same source</a> and <a href="#methods">methods section</a>.</p>
<p><a role="button" href="/share">share this page</a> <a href="/reply?id=999">reply</a>
<a href="https://news.ycombinator.com/user?id=author">author</a></p>
<section id="methods"><h2>Methods</h2><p>Every substantive paragraph remains available.</p></section>
<p hidden>HIDDEN-FIXTURE-MARKER</p><script>const ignored='SCRIPT-FIXTURE-MARKER';</script></main>""",
    }
