import neovim
import os
import subprocess
import json
import threading
import time
from functools import partial
from typing import List, Dict, Any  # noqa: F401

from . util import getRootPath, pathToURI, uriToPath, escape
from . logger import logger
from . RPC import RPC
from . TextDocumentItem import TextDocumentItem


@neovim.plugin
class LanguageClient:
    _instance = None  # type: LanguageClient

    def __init__(self, nvim):
        logger.info('__init__')
        self.nvim = nvim
        self.server = None
        self.capabilities = {}
        self.textDocuments = {}  # type: Dict[str, TextDocumentItem]
        type(self)._instance = self
        self.serverCommands = self.nvim.eval(
                "get(g:, 'LanguageClient_serverCommands', {})")

    def asyncEval(self, expr: str) -> None:
        self.nvim.async_call(lambda: self.nvim.eval(expr))

    def asyncCommand(self, cmds: str) -> None:
        self.nvim.async_call(lambda: self.nvim.command(cmds))

    def asyncEcho(self, message: str) -> None:
        message = escape(message)
        self.asyncCommand("echo '{}'".format(message))

    def getPos(self, mark=".") -> List[int]:
        _, line, character, _ = self.nvim.call("getpos", mark)
        return [line - 1, character - 1]

    def getArgs(self, argsL: List, keys: List) -> List:
        if len(argsL) == 0:
            args = {}  # type: Dict[str, Any]
        else:
            args = argsL[0]

        pos = []  # type: List[int]

        res = []
        for k in keys:
            if k == "uri":
                v = args.get("uri", pathToURI(self.nvim.current.buffer.name))
            elif k == "line":
                pos = self.getPos()
                v = args.get("line", pos[0])
            elif k == "character":
                v = args.get("character", pos[1])
            else:
                v = args.get(k, None)
            res.append(v)

        return res

    def applyChanges(self, changes: Dict, curPos: Dict) -> None:
        for uri, edits in changes.items():
            self.asyncCommand("edit {}".format(uriToPath(uri)))
            for edit in edits:
                line = edit['range']['start']['line'] + 1
                character = edit['range']['start']['character'] + 1
                newText = edit['newText']
                cmd = "normal! {}G{}|cw{}".format(line, character, newText)
                self.asyncCommand(cmd)
        time.sleep(0.05)
        self.asyncCommand("buffer {}".format(uriToPath(curPos["uri"])))
        line = curPos["line"] + 1
        character = curPos["character"] + 1
        self.asyncCommand("normal! {}G{}|".format(line, character))

    def alive(self, warn=True) -> bool:
        if self.server is None or self.server.poll() is not None:
            if warn:
                self.asyncEcho("Language client is not running. Try :LanguageClientStart")  # noqa: E501
            return False
        return True

    @neovim.command('LanguageClientStart')
    def start(self) -> None:
        if self.alive(warn=False):
            self.asyncEcho("Language client has already started.")
            return

        logger.info('start')

        filetype = self.nvim.eval('&filetype')
        if not filetype or filetype not in self.serverCommands:
            self.asyncEcho("No language server commmand found for type: {}.".format(filetype))  # noqa: E501
            return
        command = self.serverCommands[filetype]

        self.server = subprocess.Popen(
            # ["/bin/bash", "/opt/rls/wrapper.sh"],
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True)
        time.sleep(0.1)
        if not self.alive(warn=False):
            self.asyncEcho(
                    "Failed to start server. "
                    "{}".format(self.server.stderr.readlines()))
            return

        self.rpc = RPC(
            self.server.stdout, self.server.stdin,
            self.handleRequestOrNotification,
            self.handleRequestOrNotification,
            self.handleError)
        threading.Thread(
                target=self.rpc.serve, name="RPC Server", daemon=True).start()
        self.initialize([])
        self.textDocument_didOpen()

    @neovim.function('LanguageClient_initialize')
    def initialize(self, args: List) -> None:
        # {rootPath?: str, cb?}
        if not self.alive():
            return

        logger.info('initialize')

        rootPath, cb = self.getArgs(args, ["rootPath", "cb"])
        if rootPath is None:
            rootPath = getRootPath(self.nvim.current.buffer.name)
        if cb is None:
            cb = self.handleInitializeResponse

        self.rpc.call('initialize', {
            "processId": os.getpid(),
            "rootPath": rootPath,
            "rootUri": pathToURI(rootPath),
            "capabilities": {},
            "trace": "verbose"
            }, cb)

    def handleInitializeResponse(self, result: Dict) -> None:
        self.capabilities = result['capabilities']
        self.asyncEcho("LanguageClient initialization finished.")

    @neovim.autocmd('BufReadPost', pattern="*")
    def textDocument_didOpen(self) -> None:
        if not self.alive(warn=False):
            return

        logger.info('textDocument/didOpen')

        languageId = self.nvim.eval('&filetype')
        if not languageId or languageId not in self.serverCommands:
            return
        uri, = self.getArgs({}, ["uri"])
        text = str.join(
                "",
                [l + "\n" for l in self.nvim.call("getline", 1, "$")])

        textDocumentItem = TextDocumentItem(uri, languageId, text)
        self.textDocuments[uri] = textDocumentItem

        self.rpc.notify('textDocument/didOpen', {
            "textDocument": {
                "textDocument": textDocumentItem.__dict__
                }
            })

    @neovim.function('LanguageClient_textDocument_didClose')
    def textDocument_didClose(self, args: List) -> None:
        # {uri?: str}
        if not self.alive():
            return

        logger.info('textDocument/didClose')

        uri, = self.getArgs(args, ["uri"])
        del self.textDocuments[uri]

        self.rpc.notify('textDocument/didClose', {
            "textDocument": {
                "uri": uri
                }
            })

    @neovim.function('LanguageClient_textDocument_hover')
    def textDocument_hover(self, args: List) -> None:
        # {uri?: str, line?: int, character?: int, cb?}
        if not self.alive():
            return

        logger.info('textDocument/hover')

        uri, line, character, cb = self.getArgs(
            args, ["uri", "line", "character", "cb"])
        if cb is None:
            cb = self.handleTextDocumentHoverResponse

        self.rpc.call('textDocument/hover', {
            "textDocument": {
                "uri": uri
                },
            "position": {
                "line": line,
                "character": character
                }
            }, cb)

    def markedStringToString(self, s: Any) -> str:
        if isinstance(s, str):
            return s
        else:
            return s["value"]

    def handleTextDocumentHoverResponse(self, result: Dict) -> None:
        contents = result["contents"]
        value = ''
        if isinstance(contents, list):
            for markedString in result['contents']:
                value += self.markedStringToString(markedString)
        else:
            value += self.markedStringToString(contents)
        self.asyncEcho(value)

    # TODO
    # completionItem/resolve
    # textDocument/signatureHelp
    # textDocument/references
    # textDocument/codeAction

    @neovim.function('LanguageClient_textDocument_definition')
    def textDocument_definition(self, args: List) -> None:
        # {uri?: str, line?: int, character?: int, cb?}
        if not self.alive():
            return

        logger.info('textDocument/definition')

        uri, line, character, cb = self.getArgs(
            args, ["uri", "line", "character", "cb"])
        if cb is None:
            cb = self.handleTextDocumentDefinitionResponse

        self.rpc.call('textDocument/definition', {
            "textDocument": {
                "uri": uri
                },
            "position": {
                "line": line,
                "character": character
                }
            }, cb)

    def handleTextDocumentDefinitionResponse(self, result: List) -> None:
        if len(result) > 1:
            logger.warn(
                "Handling multiple definition are not implemented yet.")

        defn = result[0]
        self.asyncCommand("edit {}".format(uriToPath(defn["uri"])))
        line = defn['range']['start']['line'] + 1
        character = defn['range']['start']['character'] + 1
        self.asyncCommand("normal! {}G{}|".format(line, character))

    @neovim.function('LanguageClient_textDocument_rename')
    def textDocument_rename(self, args: List) -> None:
        # {uri?: str, line?: int, character?: int, newName?: str, cb?}
        if not self.alive():
            return

        logger.info('textDocument/rename')

        uri, line, character, newName, cb = self.getArgs(
            args, ["uri", "line", "character", "newName", "cb"])
        if newName is None:
            self.nvim.call("inputsave")
            newName = self.nvim.call("input", "Rename to: ")
            self.nvim.call("inputrestore")
        if cb is None:
            cb = partial(
                    self.handleTextDocumentRenameResponse,
                    curPos={"line": line, "character": character, "uri": uri})

        self.rpc.call('textDocument/rename', {
            "textDocument": {
                "uri": uri
                },
            "position": {
                "line": line,
                "character": character,
                },
            "newName": newName
            }, cb)

    def handleTextDocumentRenameResponse(
            self, result: Dict, curPos: Dict) -> None:
        changes = result['changes']
        self.applyChanges(changes, curPos)

    @neovim.function('LanguageClient_textDocument_documentSymbol')
    def textDocument_documentSymbol(self, args: List) -> None:
        # {uri?: str, cb?}
        if not self.alive():
            return

        logger.info('textDocument/documentSymbol')

        uri, cb = self.getArgs(args, ["uri", "cb"])
        if cb is None:
            if self.nvim.eval("get(g:, 'loaded_fzf', 0)") == 1:
                cb = self.handleTextDocumentDocumentSymbolResponse
            else:
                logger.warn("FZF not loaded.")

        self.rpc.call('textDocument/documentSymbol', {
            "textDocument": {
                "uri": uri
                }
            }, cb)

    def handleTextDocumentDocumentSymbolResponse(self, symbols: List) -> None:
        source = []
        for sb in symbols:
            name = sb["name"]
            start = sb["location"]["range"]["start"]
            line = start["line"] + 1
            character = start["character"] + 1
            entry = "{}:{}:\t{}".format(line, character, name)
            source.append(entry)
        self.asyncCommand("""
call fzf#run(fzf#wrap({{
    'source': {},
    'sink': function('LanguageClient#FZFSinkDocumentSymbol')
    }}))
""".replace("\n", "").format(json.dumps(source)))
        self.nvim.async_call(lambda: self.nvim.feedkeys("i"))

    @neovim.function('LanguageClient_FZFSinkDocumentSymbol')
    def fzfSink(self, args: List) -> None:
        splitted = args[0].split(":")
        line = int(splitted[0])
        character = int(splitted[1])
        self.asyncCommand("normal! {}G{}|".format(line, character))

    @neovim.function('LanguageClient_workspace_symbol')
    def workspace_symbol(self, args: List) -> None:
        if not self.alive():
            return
        logger.info("workspace/symbol")

        query, cb = self.getArgs(args, ["query", "cb"])
        if cb is None:
            cb = self.handleWorkspaceSymbolResponse

        self.rpc.call('workspace/symbol', {
            "query": query
            }, cb)

    def handleWorkspaceSymbolResponse(self, result: list) -> None:
        self.asyncEcho("{} symbols".format(len(result)))

    @neovim.autocmd("TextChanged", pattern="*")
    def textDocument_autocmdTextChanged(self):
        self.textDocument_didChange()

    @neovim.autocmd("TextChangedI", pattern="*")
    def textDocument_autocmdTextChangedI(self):
        self.textDocument_didChange()

    def textDocument_didChange(self) -> None:
        if not self.alive(warn=False):
            return
        logger.info("textDocument/didChange")

        uri, = self.getArgs({}, ["uri"])
        if uri not in self.textDocuments:
            return
        newText = str.join(
                "",
                [l + "\n" for l in self.nvim.call("getline", 1, "$")])
        version, changes = self.textDocuments[uri].change(newText)

        self.rpc.notify("textDocument/didChange", {
            "textDocument": {
                "uri": uri,
                "version": version
                },
            "contentChanges": changes
            })

    @neovim.autocmd("BufWritePost", pattern="*")
    def textDocument_didSave(self) -> None:
        if not self.alive(warn=False):
            return
        logger.info("textDocument/didSave")

        uri, = self.getArgs({}, ["uri"])

        self.rpc.notify("textDocument/didSave", {
            "textDocument": {
                "uri": uri
                }
            })

    @neovim.function("LanguageClient_textDocument_completion")
    def textDocument_completion(self, args: List) -> List:
        if not self.alive():
            return []
        logger.info("textDocument/completion")

        uri, line, character = self.getArgs(args, ["uri", "line", "character"])

        return self.rpc.call('textDocument/completion', {
            "textDocument": {
                "uri": uri
                },
            "position": {
                "line": line,
                "character": character
                }
            })

    # FIXME: python infinite loop after this call.
    @neovim.function("LanguageClient_exit")
    def exit(self, args: List) -> None:
        # {uri?: str}
        if not self.alive():
            return
        logger.info("exit")

        self.rpc.notify("exit", {})

    def textDocument_publishDiagnostics(self, params) -> None:
        for diagnostic in params['diagnostics']:
            message = diagnostic['message'].replace("\n", ". ")
            self.asyncEcho(message)

    def handleRequestOrNotification(self, message) -> None:
        method = message['method'].replace('/', '_')
        if hasattr(self, method):
            try:
                getattr(self, method)(message['params'])
            except:
                logger.exception("Exception in handle.")
        else:
            logger.warn('no handler implemented for ' + method)

    def handleError(self, message) -> None:
        self.asyncEcho(json.dumps(message))