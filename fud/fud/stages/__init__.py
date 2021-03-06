"""The definitions of fud stages."""

import subprocess
from enum import Enum
from tempfile import NamedTemporaryFile, TemporaryFile
from pathlib import Path
import sys
import logging as log

from ..utils import is_debug


class SourceType(Enum):
    """
    Enum capturing the kind of source this is.
    Path: A string for a file path. Indicates we want to write/read
          from a path.
    File: A file object that is directly writable/readable. Indicates
          an already open path or a pipe.
    Nothing: An empty source. This is neither readable nor writable.
    """
    Path = 1,
    File = 2,
    Nothing = 3


class Source:
    def __init__(self, data, source_type):
        self.data = data
        self.source_type = source_type

    def to_pipe(self):
        """Converts an arbitrary source into a SourceType.File"""
        if self.source_type == SourceType.Path:
            Path(self.data).touch()
            self.data = open(self.data, 'r+')
            self.source_type = SourceType.File
        elif self.source_type == SourceType.File:
            pass
        elif self.source_type == SourceType.Nothing:
            self.data = sys.stdout
            self.source_type = SourceType.File

    def to_path(self):
        """Converts an arbitrary source into a SourceType.Path"""
        if self.source_type == SourceType.Path:
            pass
        elif self.source_type == SourceType.File:
            with NamedTemporaryFile('wb', delete=False) as tmpfile:
                for line in self.data:
                    tmpfile.write(line)
                self.data = tmpfile.name
                self.source_type = SourceType.Path
        elif self.source_type == SourceType.Nothing:
            pass

    def __repr__(self):
        return f"<Source {self.source_type} {self.data}>"


class Stage:
    """
    Represents a stage in the execution pipeline. This encompasses
    the process of transforming one file type into the next.
    `name`: The name of this stage.
    `target_stage`: The name of the stage generated by this.
    `config`: The configuration object read from disk + any
              dynamic modifications made with `-s`.
    `description`: Description of this stage
    """
    def __init__(self,
                 name,
                 target_stage,
                 config,
                 description):
        self.name = name
        self.target_stage = target_stage
        self.global_config = config.config['global']
        self.stage_config = config.find(['stages', self.name])
        self.cmd = self.stage_config['exec']
        self.description = description

    def _define(self):
        """Returns a list of steps to execute."""
        return []

    def transform(self, input_src, dry_run=False, last=False):
        steps = self._define()
        ctx = {}

        prev_out = input_src
        ret = None
        err = None
        if dry_run:
            print(f'   + {self.name}')
        # loop until last step
        for i, step in enumerate(steps):
            last = last and i == len(steps) - 1
            res = step.run(prev_out, ctx=ctx, dry_run=dry_run, last=last)
            (prev_out, err, ret) = res
            err_msg = self.check_exit(ret, err)
            if err_msg is not None:
                err = err_msg
                break

        return (prev_out, err, ret)

    def check_exit(self, retcode, stderr):
        if retcode != 0:
            msg = f"Stage '{self.name}' had a non-zero exit code."
            n_dashes = (len(msg) - len(' stderr ')) // 2
            dashes = "-" * n_dashes + ' stderr ' + '-' * n_dashes
            return '\n'.join([msg, dashes, stderr])

        return None


class Step:
    """
    A subdivision of a stage. This allows for more complicated stages.
    Also automates the process of launching subprocesses to run shell commands.
    `desired_input_type`: The input type that this step expects.
    `last_context`: Context to add when this step is the last in the pipeline.
                    This allows for different behavior when this step is
                    outputting directly to a terminal rather than to
                    another process.
    """
    def __init__(self, desired_input_type, last_context={}):
        self.func = None
        self.description = "No description provided."
        self.desired_input_type = desired_input_type
        self.last_context = last_context

    def run(self, input_src, ctx={}, dry_run=False, last=False):
        if dry_run:
            print(f'     - {self.description}')
            return (None, None, 0)
        else:
            # convert input type to desired input type
            if self.desired_input_type == SourceType.Path:
                input_src.to_path()
            elif self.desired_input_type == SourceType.File:
                input_src.to_pipe()

            # update context with step specific items
            if last:
                for key, value in self.last_context.items():
                    ctx[key] = value
            else:
                for key in self.last_context.keys():
                    ctx[key] = ''

            return self.func(input_src, ctx)

    def set_cmd(self, cmd):
        def f(inp, ctx):
            nonlocal cmd
            proc = None
            stdout = TemporaryFile()
            stderr = None
            if not is_debug():
                stderr = TemporaryFile()
            if inp.source_type == SourceType.Path:
                ctx['input_path'] = inp.data
                log.debug('  - [*] {}'.format(cmd.format(ctx=ctx)))
                proc = subprocess.Popen(
                    cmd.format(ctx=ctx),
                    shell=True,
                    stdout=stdout,
                    stderr=stderr,
                )
            else:
                log.debug('  - [*] pipe: {}'.format(cmd.format(ctx=ctx)))
                proc = subprocess.Popen(
                    cmd.format(ctx=ctx),
                    shell=True,
                    stdin=inp.data,
                    stdout=stdout,
                    stderr=stderr
                )

            proc.wait()
            # move read pointers back to the beginning
            stdout.seek(0)

            stderr_text = ''
            if not is_debug():
                stderr.seek(0)
                stderr_text = stderr.read().decode('UTF-8')

            return (
                Source(stdout, SourceType.File),
                stderr_text,
                proc.returncode
            )
        self.func = f
        self.description = cmd

    def set_func(self, func, description):
        def f(inp, ctx):
            log.debug(description)
            return func(inp, ctx)
        self.func = f
        self.description = description
