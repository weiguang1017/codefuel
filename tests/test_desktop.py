from types import SimpleNamespace

import desktop


class Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


class Window:
    def __init__(self):
        self.events = SimpleNamespace(closing=Event())

    def evaluate_js(self, *_args, **_kwargs):
        raise AssertionError('native close must not wait for renderer JavaScript')

    def create_confirmation_dialog(self, *_args, **_kwargs):
        raise AssertionError('native close must not open a blocking confirmation dialog')


def test_window_close_does_not_block_on_renderer_or_confirmation(monkeypatch):
    window = Window()
    monkeypatch.setattr(desktop.webview, 'create_window', lambda *_args, **_kwargs: window)

    desktop.build_window('http://127.0.0.1:8765')

    assert len(window.events.closing.handlers) == 1
    assert window.events.closing.handlers[0]() is True
