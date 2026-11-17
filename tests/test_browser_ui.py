"""The page itself, in a real browser: what the server tests cannot see.

Each test starts a server on a temporary workspace (never your own sessions or settings), opens the
page in headless Chrome and drives it the way a person would, or through the same event handler the
live stream feeds. Skipped when Playwright or Chrome is not installed.
"""

import contextlib
import os
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from test_webui import http, make_session_with_fake, start_server, text_msg  # noqa: E402

sync_api = pytest.importorskip("playwright.sync_api")


@contextlib.contextmanager
def page_on_server(width=1500, height=950, script=None):
    """A fresh server and the page open on it; yields (page, base, app, tmp, errors)."""
    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        sid = None
        if script is not None:
            sid, _ = make_session_with_fake(base, tmp, script)
        try:
            with sync_api.sync_playwright() as pw:
                try:
                    browser = pw.chromium.launch(channel="chrome")
                except Exception:  # noqa: BLE001 - no Chrome: Playwright's own Chromium, if it is installed
                    try:
                        browser = pw.chromium.launch()
                    except Exception as exc:  # noqa: BLE001
                        pytest.skip(f"no browser for the page tests: {exc}")
                page = browser.new_page(viewport={"width": width, "height": height})
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(base + "/#shot", wait_until="load")
                page.wait_for_function("typeof S !== 'undefined' && !!S.config")
                yield page, base, app, tmp, errors, sid
                browser.close()
        finally:
            server.shutdown()


def live_turn(page):
    """A new session showing a turn, as the live stream starts one."""
    page.evaluate("""() => { newSession(); document.getElementById('hello').classList.add('hidden');
                              addUserMsg('Why is the parser slow?'); handleEvent('turn_start', {}); }""")


def test_every_menu_settings_page_and_pane_opens_without_an_error():
    with page_on_server() as (page, base, app, tmp, errors, sid):
        done = page.evaluate("""async () => {
          var wait = ms => new Promise(r => setTimeout(r, ms)), failed = [];
          var run = async (name, fn) => { try { await fn(); } catch (e) { failed.push(name + ': ' + e.message); } await wait(60); closeMenus(); };
          for (var m of Object.keys(MENU_BUILDERS)) await run('menu ' + m, () => openMenu(m));
          for (var p of Object.keys(PAGES)) await run('settings ' + p, () => openSettings(p));
          await run('close settings', () => closeSettings());
          for (var t of [toggleTasks, togglePlan, () => toggleBrowser(), () => showActivity(true), () => showActivity(false), openShortcuts])
            await run(t.name || 'pane', t);
          return failed;
        }""")
        assert done == [] and errors == [], (done, errors)


def test_text_between_steps_stays_and_each_group_sums_itself_up():
    with page_on_server() as (page, base, app, tmp, errors, sid):
        live_turn(page)
        page.evaluate("""() => {
          handleEvent('thinking', {text: 'Look at parse.py first.', final: true, secs: 12});
          handleEvent('tool_call', {name: 'read', args: {path: 'src/parse.py'}});
          handleEvent('tool_result', {name: 'read', result: 'x'});
          handleEvent('token', {text: 'The loop copies each row. Let me check the tests'});
          handleEvent('tool_call', {name: 'bash', args: {command: 'pytest -q tests/test_parse.py', description: 'Run the parser tests'}});
        }""")
        state = page.evaluate("""() => ({
          thought: document.querySelector('.thought .tht').textContent,
          groups: [...document.querySelectorAll('.work > summary .wt')].map(e => e.textContent),
          kept: document.querySelector('.reply.interim .md').textContent,
          live: document.getElementById('live-text').textContent,
          cursorInline: !!document.querySelector('.reply.interim .cursor') === false
        })""")
        assert state["thought"] == "Thought for 12s", state
        assert state["groups"][0] == "Read a file" and state["groups"][1] == "Running the parser tests", state
        assert "copies each row" in state["kept"], "the words before the step stay"
        assert state["live"] == "Running the parser tests" and "pytest" not in state["live"], \
            "the running line says what it does in words; the command is only in the step's detail"
        assert state["cursorInline"], "a sealed piece of text has no cursor"
        page.evaluate("handleEvent('token', {text: 'Two tests fail on quoted commas'})")
        inside = page.evaluate("!!document.querySelector('.reply:not(.interim) .md p .cursor')")
        assert inside, "the cursor sits at the end of the line being written, not on a line of its own"
        assert errors == []


def test_a_turn_stopped_mid_thought_settles():
    with page_on_server() as (page, base, app, tmp, errors, sid):
        live_turn(page)
        page.evaluate("handleEvent('thinking', {text: 'Hmm, the parser', final: false})")
        assert page.evaluate("document.querySelector('.thought').classList.contains('live')")
        page.evaluate("handleEvent('turn_end', {elapsed_ms: 900})")
        state = page.evaluate("({live: document.querySelector('.thought').classList.contains('live'), "
                              "label: document.querySelector('.thought .tht').textContent})")
        assert state == {"live": False, "label": "Thought"}, state


def test_live_indicators_move_and_the_spark_keeps_its_size():
    with page_on_server() as (page, base, app, tmp, errors, sid):
        live_turn(page)
        page.evaluate("""() => { handleEvent('tool_call', {name: 'bash', args: {command: 'ls', description: 'List the files'}});
                                 S.workStart = Date.now(); document.getElementById('livebar').classList.remove('hidden'); tickLive(); }""")
        page.wait_for_timeout(700)   # the bar's entrance has settled
        result = page.evaluate("""async () => {
          var running = [...new Set(document.getAnimations().filter(a => a.playState === 'running').map(a => a.animationName))];
          var xs = new Set();
          for (var i = 0; i < 20; i++) { xs.add(Math.round(document.getElementById('live-text').getBoundingClientRect().left)); await new Promise(r => setTimeout(r, 70)); }
          return {running: running, positions: [...xs]};
        }""")
        for name in ("spin", "sparkle", "shimmer"):
            assert name in result["running"], result
        assert len(result["positions"]) == 1, "the words beside the spark must not move: " + str(result["positions"])


def test_activity_stays_closed_when_a_message_is_sent():
    with page_on_server() as (page, base, app, tmp, errors, sid):
        page.evaluate("showActivity(false)")
        live_turn(page)
        assert page.evaluate("document.getElementById('inspector').classList.contains('hidden')"), \
            "a turn starting does not open Activity"
        page.evaluate("showActivity(true)")
        page.reload(wait_until="load")
        page.wait_for_function("typeof S !== 'undefined' && !!S.config")
        page.wait_for_timeout(300)
        assert not page.evaluate("document.getElementById('inspector').classList.contains('hidden')"), \
            "left open, it is open again after a reload"


def test_split_diff_pairs_removed_and_added_lines():
    with page_on_server() as (page, base, app, tmp, errors, sid):
        rows = page.evaluate("""() => {
          var d = renderSplitDiff('@@ -1,4 +1,4 @@\\n a\\n-b\\n-c\\n+B\\n d\\n', 'x.py');
          return [...d.querySelectorAll('.sl')].map(r => [...r.querySelectorAll('.sc')].map(c => c.className.replace('sc ', '') + ':' + c.textContent));
        }""")
        assert rows == [["same:1a", "same:1a"], ["del:2b", "add:2B"], ["del:3c", "none:"], ["same:4d", "same:3d"]], rows


def test_keyboard_shortcuts_can_be_changed():
    with page_on_server() as (page, base, app, tmp, errors, sid):
        is_open = "document.getElementById('cmdk-modal').classList.contains('open')"
        page.keyboard.press("Control+k")
        assert page.evaluate(is_open)
        page.keyboard.press("Escape")
        page.evaluate("openSettings('keyboard')")
        page.locator(".keyrow .keybtn").first.click()
        page.locator(".keyrow .keybtn").nth(1).click()   # a second one while the first waits: only one listens
        page.keyboard.press("Control+j")
        page.wait_for_timeout(400)
        labels = page.evaluate("[...document.querySelectorAll('.keyrow .keybtn')].slice(0, 2).map(b => b.textContent)")
        assert labels == ["Ctrl K", "Ctrl J"], labels
        page.evaluate("closeSettings()")
        page.keyboard.press("Control+j")
        assert not page.evaluate(is_open), "Ctrl+J is New session now, not search"
        status, s = http("GET", base + "/api/settings")
        assert s["ui"]["keys"] == {"new-session": "Ctrl+J"}, s["ui"]["keys"]


def test_a_mermaid_block_is_drawn_as_a_diagram():
    with page_on_server() as (page, base, app, tmp, errors, sid):
        live_turn(page)
        page.evaluate("""() => { handleEvent('token', {text: 'Here:\\n\\n```mermaid\\nflowchart LR\\n  A[Box] --> B[Server]\\n```', final: true});
                                 handleEvent('turn_end', {elapsed_ms: 500}); }""")
        page.wait_for_selector(".diagram svg", timeout=15000)
        width = page.evaluate("document.querySelector('.diagram svg').getBoundingClientRect().width")
        assert width > 100, "drawn at its size, not squeezed to an icon: %s" % width
        assert page.evaluate("document.querySelector('.mermaid-src').classList.contains('hidden')")


def test_search_finds_a_formatted_reply_and_jumps_to_it():
    # the page asks the model for its own small things too (a suggested next prompt): enough replies for all
    script = [text_msg("The fix is in **parse_rows**: it copies `each row` twice before splitting quoted commas.")] * 6
    with page_on_server(script=script) as (page, base, app, tmp, errors, sid):
        http("POST", f"{base}/api/sessions/{sid}/message", {"text": "where is the bug?"})
        for _ in range(100):
            if any(i["kind"] == "assistant" for i in app.store.items(sid)):
                break
            page.wait_for_timeout(100)
        page.evaluate("newSession()")
        page.keyboard.press("Control+k")
        page.keyboard.type("copies twice")
        page.wait_for_selector("#cmdk-list .mi:has-text('copies')", timeout=5000)
        page.locator("#cmdk-list .mi:has-text('copies')").first.click()
        page.wait_for_selector("#chat .reply.found", timeout=5000)


def test_another_session_does_not_show_the_last_ones_agent_browser():
    with page_on_server() as (page, base, app, tmp, errors, sid):
        page.evaluate("toggleBrowser(); onBrowserView({url: 'http://localhost:5173/', n: 1})")
        assert page.evaluate("S.br.mode") == "agent"
        page.evaluate("newSession()")
        state = page.evaluate("({agent: S.br.agent, disabled: document.getElementById('br-tab-agent').disabled, mode: S.br.mode})")
        assert state == {"agent": None, "disabled": True, "mode": "you"}, state


def test_diagrams_take_the_new_theme():
    with page_on_server() as (page, base, app, tmp, errors, sid):
        page.evaluate("applyTheme(THEMES.warm)")
        live_turn(page)
        page.evaluate("""() => { handleEvent('token', {text: '```mermaid\\nflowchart LR\\n  A --> B\\n```', final: true});
                                 handleEvent('turn_end', {elapsed_ms: 500}); }""")
        page.wait_for_selector(".diagram svg", timeout=15000)
        node_fill = "getComputedStyle(document.querySelector('.diagram svg .node rect, .diagram svg .node polygon')).fill"
        dark = page.evaluate(node_fill)
        page.evaluate("applyTheme(THEMES.paper)")
        page.wait_for_function("document.querySelectorAll('.diagram svg').length === 1 && "
                               "getComputedStyle(document.querySelector('.diagram svg .node rect, .diagram svg .node polygon')).fill !== %r" % dark,
                               timeout=15000)
        assert page.evaluate("document.querySelectorAll('.diagram').length") == 1, "redrawn, not added twice"
        assert errors == []


def test_opening_a_long_session_lands_at_the_very_end():
    with page_on_server(script=[]) as (page, base, app, tmp, errors, sid):
        gap = page.evaluate(r"""async (sid) => {
          var md = '## Notes\n\n' + 'A line of findings about the parser and its rows. '.repeat(30) +
                   '\n\n```python\n' + 'x = 1\n'.repeat(25) + '```\n\n- one\n- two\n- three';
          var entries = [];
          for (var i = 0; i < 80; i++) { entries.push({kind: 'user', text: 'Question ' + i}); entries.push({kind: 'assistant', text: md}); }
          var real = api;
          api = function (m, u, b) { return /\/history$/.test(u) ? Promise.resolve({transcript: entries, seq: 0}) : real(m, u, b); };
          newSession(); openSession(sid);
          await new Promise(r => setTimeout(r, 2500));
          var w = document.getElementById('chat-wrap');
          return w.scrollHeight - w.scrollTop - w.clientHeight;
        }""", sid)
        assert gap < 4, "a reopened session shows its last message, not a little above it: %s px short" % gap


def test_activity_closed_by_another_pane_stays_closed():
    with page_on_server() as (page, base, app, tmp, errors, sid):
        page.evaluate("showActivity(true); toggleTasks()")   # Tasks takes Activity's place
        page.reload(wait_until="load")
        page.wait_for_function("typeof S !== 'undefined' && !!S.config")
        page.evaluate("newSession()")
        page.wait_for_timeout(300)
        assert page.evaluate("document.getElementById('inspector').classList.contains('hidden')")


def test_the_page_comes_back_as_it_was_after_a_restart():
    # what was open, what was typed but not sent, and the pane beside the chat (keep.js)
    with page_on_server(script=[text_msg("hello")]) as (page, base, app, tmp, errors, sid):
        page.evaluate(f"openSession('{sid}')")
        page.wait_for_function(f"S.snap && S.snap.id === '{sid}'")
        page.fill("#input", "half a thought I had not sent")
        page.evaluate("toggleTerm()")
        page.wait_for_timeout(600)   # the draft is kept a moment after typing stops
        session = app.get_session(sid)   # a session with a conversation (an empty one is tidied away)
        session._record({"kind": "user", "text": "hi", "turn": "t1"})
        session._record({"kind": "assistant", "text": "hello"})
        session._save_transcript()
        app.refresh_index_entry(session)
        app.sessions.clear()   # the server forgets its open sessions, as after a crash
        page.goto(base + "/", wait_until="load")
        page.wait_for_function("typeof S !== 'undefined' && S.snap && S.snap.id", timeout=15000)
        assert page.evaluate("S.current") == sid
        assert page.input_value("#input") == "half a thought I had not sent"
        page.wait_for_function("paneOpen('term-pane')", timeout=5000)
        assert not errors, errors


def test_a_reply_cut_off_by_a_crash_says_so_and_can_carry_on():
    with page_on_server() as (page, base, app, tmp, errors, sid):
        session = app.create_session(tmp)
        session._record({"kind": "user", "text": "Make the game faster", "turn": "t1"})
        session._record({"kind": "tool", "name": "read", "args": {"path": "game.py"}, "result": "print(1)"})
        app.refresh_index_entry(session)
        sid = session.id
        app.sessions.pop(sid)   # the server stopped mid-reply
        page.evaluate(f"S.list = [{{ id: '{sid}', title: 'x' }}]; openSession('{sid}')")
        page.wait_for_selector(".interrupted .btn.primary", timeout=15000)
        text = page.inner_text(".interrupted")
        assert "cut off" in text and "Continue" in text, text
        reopened = app.get_session(sid)
        assert reopened.transcript[-1]["kind"] == "interrupted" and reopened.transcript[-1]["text"] == "Make the game faster"
        assert any(e.get("kind") == "tool" for e in reopened.transcript), "the steps taken are kept"
        # opened again later: said once, not twice
        app.sessions.pop(sid)
        again = app.reopen_session(sid)
        assert [e["kind"] for e in again.transcript].count("interrupted") == 1
        assert not errors, errors
