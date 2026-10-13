"""Leitura de trailers e co-autores em mensagens de commit."""
from __future__ import annotations

import re

_TRAILER = re.compile(r"^([A-Za-z][A-Za-z0-9-]*):\s*(.+?)\s*$")


def parse_trailers(message: str) -> list[str]:
    """Devolve os trailers do último parágrafo da mensagem, como «Chave: valor».

    Segue a convenção do git: os trailers estão no último bloco separado por linha em branco
    e cada linha tem a forma «Chave: valor». Linhas que não são trailers nesse bloco
    (ex.: «🤖 Generated with Claude Code») são ignoradas em vez de invalidarem o bloco,
    porque as ferramentas de IA costumam misturá-las.
    """
    if not message:
        return []
    blocks = re.split(r"\n\s*\n", message.strip())
    if len(blocks) < 2:
        return []
    out = []
    for line in blocks[-1].splitlines():
        m = _TRAILER.match(line.strip())
        if m and not m.group(2).startswith("//"):
            out.append(f"{m.group(1)}: {m.group(2)}")
    return out


def co_authors(trailers: list[str]) -> list[str]:
    return [t.split(":", 1)[1].strip() for t in trailers if t.lower().startswith("co-authored-by:")]
