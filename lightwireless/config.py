"""Config em dicionario, mesclado sobre o padrao campo a campo.

Mora em config.json na pasta do programa (portable). Opcao nova entra so no
PADRAO. SEGREDO: guarda a chave da lampada (e as credenciais da nuvem, se ele
usou a busca): nunca vai pro git nem pro zip.

O IP e cache, nao configuracao: o DHCP muda o endereco sozinho. Quem conecta
tenta o guardado e, se nao responder, procura na rede e regrava.

Sem config.json mas com um devices.json (do assistente do tinytuya) ao lado,
ele e convertido na primeira execucao.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import shutil
import threading
import uuid
from pathlib import Path

from .caminhos import arquivo

log = logging.getLogger(__name__)
ARQUIVO = arquivo("config.json")
ARQUIVO_WIZARD = arquivo("devices.json")

PADRAO = {
    "id": "",
    "chave": "",
    "nome": "",
    "ip": "",
    "nuvem": {},                     # regiao, api_id, api_secret (busca da chave)
    "ultima_temperatura": 400,       # 0 quente .. 1000 frio (DP 23)
    "timer_minutos": 60,
    # agenda: {"id", "hora": "22:00", "dias": [0..6] (0 = segunda), "acao", "ligado"}
    "agenda": [],
    "atalhos": {},
    "opcoes": {
        "iniciar_com_windows": False,
        "comecar_na_bandeja": False,
        "ao_abrir": "nada",          # nada | ligar | desligar | t:<0..1000>
        "ao_sair": "nada",           # nada | ligar | desligar | t:<0..1000> (segurar o X)
    },
    "janela": {"x": None, "y": None},
}

_trava = threading.Lock()            # a thread da lampada tambem grava (IP novo)


def _mesclar(padrao, lido):
    if isinstance(padrao, dict) and isinstance(lido, dict):
        saida = dict(padrao)
        for k, v in lido.items():
            saida[k] = _mesclar(padrao[k], v) if k in padrao else v
        return saida
    if lido is None or (padrao is not None and type(lido) is not type(padrao)
                        and not (isinstance(padrao, (int, float)) and isinstance(lido, (int, float)))):
        return copy.deepcopy(padrao)
    return lido


def _migrar(lido: dict) -> dict:
    """Opcoes da v0.1.0 viram as de agora (quem ja usava nao perde a escolha)."""
    op = lido.get("opcoes")
    if isinstance(op, dict):
        if "ao_abrir" not in op and op.get("acender_ao_abrir"):
            op["ao_abrir"] = "ligar"
        if "comecar_na_bandeja" not in op and op.get("abrir_janela_ao_iniciar") is False:
            op["comecar_na_bandeja"] = True
        if "ao_sair" not in op and op.get("apagar_ao_fechar"):
            op["ao_sair"] = "desligar"           # a chave "apagar ao sair" virou o menu "Ao sair"
        for velho in ("acender_ao_abrir", "abrir_janela_ao_iniciar", "apagar_ao_desligar", "apagar_ao_fechar"):
            op.pop(velho, None)
    at = lido.get("atalhos")
    if isinstance(at, dict):
        at.pop("esconder_bandeja", None)      # virou "mostrar / esconder"
        at.pop("soltar", None)                # saiu (11/out/2026): o app do celular funciona com o PC conectado
    return lido


class Config(dict):
    @classmethod
    def carregar(cls) -> "Config":
        lido = {}
        try:
            with open(ARQUIVO, encoding="utf-8") as f:
                lido = json.load(f)
        except FileNotFoundError:
            do_wizard = de_arquivo_do_wizard(ARQUIVO_WIZARD)
            if do_wizard:
                log.info("config criado a partir do devices.json")
                lido = do_wizard
        except Exception as e:
            log.warning("config.json ilegivel (%s): usando o padrao", e)
        if not isinstance(lido, dict):
            lido = {}
        cfg = cls(_mesclar(copy.deepcopy(PADRAO), _migrar(lido)))
        if lido and not ARQUIVO.exists():
            cfg.gravar()
        return cfg

    def gravar(self) -> bool:
        with _trava:
            try:
                tmp = str(ARQUIVO) + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self, f, ensure_ascii=False, indent=2)
                os.replace(tmp, ARQUIVO)
                return True
            except Exception as e:
                log.warning("nao gravei o config: %s", e)
                return False

    # -- o que a camada da lampada le --------------------------------------
    @property
    def id(self) -> str:
        return self["id"]

    @property
    def chave(self) -> str:
        return self["chave"]

    @property
    def ip(self) -> str:
        return self["ip"]

    @property
    def completo(self) -> bool:
        """Da para tentar conectar? Sem id e chave, nao."""
        return bool(self["id"] and self["chave"])

    def lembrar_ip(self, ip: str) -> None:
        if ip and ip != self["ip"]:
            log.info("IP da lampada mudou: %s -> %s", self["ip"] or "(nenhum)", ip)
            self["ip"] = ip
            self.gravar()

    def definir_lampada(self, id: str, chave: str, nome: str = "", ip: str = "",
                        nuvem: dict | None = None) -> bool:
        # .bak da versao anterior (tem a chave velha: segredo, igual ao config)
        try:
            if ARQUIVO.exists():
                with _trava:
                    shutil.copy2(ARQUIVO, str(ARQUIVO) + ".bak")
        except OSError as e:
            log.warning("sem .bak do config: %s", e)
        self["id"], self["chave"], self["nome"], self["ip"] = id, chave, nome or "", ip or ""
        if nuvem is not None:
            self["nuvem"] = dict(nuvem)
        return self.gravar()

    @property
    def opcoes(self) -> dict:
        return self["opcoes"]

    @property
    def atalhos(self) -> dict:
        return self["atalhos"]


def de_arquivo_do_wizard(caminho, id_preferido: str = "") -> dict | None:
    """Tira id/chave/nome/ip de um devices.json em qualquer lugar do disco.

    O arquivo costuma trazer a conta inteira: `id_preferido` (o aparelho que a
    varredura viu nesta rede) escolhe qual; sem ele, o primeiro. Nunca levanta
    excecao: arquivo errado escolhido por engano e caminho normal."""
    try:
        lista = json.loads(Path(caminho).read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(lista, list):
        return None
    servem = [d for d in lista if isinstance(d, dict) and d.get("id") and d.get("key")]
    if not servem:
        return None
    d = next((d for d in servem if d["id"] == id_preferido), servem[0])
    return {"id": d["id"], "chave": d["key"], "nome": d.get("name", ""), "ip": d.get("ip", "")}


def novo_id() -> str:
    return uuid.uuid4().hex[:8]
