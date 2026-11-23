#!/usr/bin/env python
# License: GPL v3

from typing import TYPE_CHECKING, Any

from kitty.types import AsyncResponse

from .base import (
    MATCH_WINDOW_OPTION,
    ArgsType,
    Boss,
    PayloadGetType,
    PayloadType,
    RCOptions,
    RemoteCommand,
    RemoteControlError,
    RemoteControlErrorWithoutTraceback,
    ResponseType,
    Window,
)

if TYPE_CHECKING:
    from kitty.cli_stub import ShellSubmitRCOptions as CLIOptions


class ShellSubmit(RemoteCommand):

    protocol_spec = __doc__ = '''
    command+/str: The command line to submit, as UTF-8 text without control characters
    match/str: A string indicating the one window to submit the command in
    expect_prompt/str: The prompt_id the request was planned against, from the window's shell_input in ls
    '''

    short_desc = 'Run one command at a verified, empty shell prompt'
    desc = (
        'Submit one command line in a window only when its shell proves, at the moment of'
        ' acceptance, that its line editor is at a fresh primary prompt with an empty buffer.'
        ' The engine checks that nothing was typed since that prompt appeared, that no command'
        ' is running and that the shell owns the terminal; the shell integration then checks'
        ' its own editor state and either accepts the command or refuses, leaving any input'
        ' untouched. Refusals are reported as errors of the form CODE: REASON. Shells'
        ' without this verification are always refused.'
        ' With no COMMAND arguments the command is read from STDIN, which keeps it out of'
        ' process listings.'
    )
    options_spec = MATCH_WINDOW_OPTION + '''\n
--expect-prompt
Refuse unless the window is still at this prompt: the :code:`prompt_id` field of
:code:`shell_input` in :code:`kitten @ ls`. Every prompt gets a new identifier, so this
binds a submission to the prompt that was inspected when the request was planned.
Without it the current prompt is accepted.


--response-timeout
type=float
default=10
The time in seconds to wait for the shell to accept or refuse the command.
'''
    args = RemoteCommand.Args(
        spec='[COMMAND ...]', json_field='command', special_parse='read_shell_submit_command(args)')
    is_asynchronous = True

    def message_to_kitty(self, global_opts: RCOptions, opts: 'CLIOptions', args: ArgsType) -> PayloadType:
        import sys
        command = ' '.join(args) if args else sys.stdin.read(4098)
        if not args and command.endswith('\n'):
            command = command[:-1]
        return {'command': command, 'match': opts.match, 'expect_prompt': opts.expect_prompt}

    def response_from_kitty(self, boss: Boss, window: Window | None, payload_get: PayloadGetType) -> ResponseType:
        from kitty.shell_input import ShellInputRefused, submit
        match = payload_get('match')
        if not match:
            raise RemoteControlError('shell-submit needs --match naming exactly one window')
        windows = list(boss.match_windows(match, window))
        if len(windows) != 1:
            raise RemoteControlErrorWithoutTraceback(f'ambiguous: --match {match} selects {len(windows)} windows; name exactly one'
                                     if windows else f'missing: no window matches {match}')
        target = windows[0]
        command = payload_get('command')
        if not isinstance(command, str):
            raise RemoteControlErrorWithoutTraceback('invalid_command: the command must be text')
        expected = payload_get('expect_prompt') or ''
        if not isinstance(expected, str):
            raise RemoteControlErrorWithoutTraceback('stale: the expected prompt must be text')
        responder = self.create_async_responder(payload_get, window)

        def respond(result: dict[str, Any] | None, error: ShellInputRefused | None) -> None:
            if error is not None:
                responder.send_error(str(error))
            else:
                import json
                responder.send_data(json.dumps(dict(result or {}, window_id=target.id)))

        try:
            submit(target, command, expected, respond)
        except ShellInputRefused as e:
            raise RemoteControlErrorWithoutTraceback(str(e)) from None
        return AsyncResponse()


shell_submit = ShellSubmit()
