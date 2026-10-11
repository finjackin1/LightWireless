"""
Quem esta na rede: acha os aparelhos Tuya SEM precisar da chave de nenhum.

POR QUE ISTO E SEPARADO DO `lampada.py`
----------------------------------------
O `lampada.py` cuida de UMA lampada ja configurada: ele sabe o identificador
e a chave dela e mantem a conversa viva. Aqui e o contrario -- nao se sabe
nada ainda, e a pergunta e "o que existe nesta rede?".

Sao momentos diferentes do programa: um e a primeira configuracao, o outro e
o uso diario. Misturar os dois faria a camada de conexao carregar codigo que
so roda uma vez na vida do usuario.

O QUE DA PARA SABER SEM A CHAVE, E O QUE NAO DA
------------------------------------------------
Aparelho Tuya se ANUNCIA sozinho na rede, de tempos em tempos, e esse anuncio
nao e secreto. Dele saem o identificador, o endereco, a versao do protocolo e
o codigo do produto. E o bastante para dizer "achei um aparelho aqui".

O que NAO sai do anuncio: a chave que autoriza conversar, e o nome que a
pessoa deu ao aparelho no aplicativo do fabricante. A chave so existe na
nuvem da Tuya -- nao ha caminho local para ela, e e por isso que a primeira
configuracao sempre vai passar por la uma vez.

Entao esta varredura nao substitui a chave. Ela serve para o programa parar
de dizer "nao achei nada" quando a lampada esta bem ali, e passar a dizer
"achei este aparelho, falta a chave dele".

DEMORA, E TEM QUE DEMORAR
--------------------------
Nao ha como pedir que os aparelhos se anunciem: o jeito e ESCUTAR ate eles
falarem sozinhos. Por isso a varredura tem um tempo de escuta em vez de uma
resposta imediata, e por isso ela nunca pode rodar na thread da janela.
"""

from __future__ import annotations

# Quanto tempo ficar escutando os anuncios de cada vez.
SEGUNDOS_PADRAO = 6

# Quantas escutas seguidas antes de desistir.
#
# MEDIDO EM 09/set/2026, e nao escolhido no chute: escutando a rede por trinta
# segundos, a lampada se anunciou de cinco em cinco segundos -- COM UM
# SILENCIO DE DEZ. Uma escuta de seis segundos que comece dentro desse
# silencio volta vazia, e foi o que aconteceu em duas das nove medicoes
# daquele dia. Sem insistir, a tela dizia "nao achei nenhum aparelho" com a
# lampada respondendo na rede: um sorteio, apresentado como diagnostico.
#
# Quatro ciclos de seis segundos dao vinte e quatro segundos de escuta no pior
# caso -- varias vezes o maior silencio observado. E o caso comum nao espera
# nada disso: quem se anuncia no primeiro ciclo aparece na tela em seis
# segundos, como antes.
CICLOS_PADRAO = 4

# A versao de protocolo desta lampada. Aparelho de outra versao aparece na
# lista assim mesmo -- esconder o que existe seria pior --, so vem marcado.
VERSAO_ESPERADA = "3.5"


def procurar(segundos: int = SEGUNDOS_PADRAO) -> list[dict]:
    """
    Escuta a rede e devolve o que se anunciou, em ordem de endereco.

    Cada achado e um dicionario simples:

        id       identificador do aparelho
        ip       endereco na rede agora (muda, e DHCP)
        versao   versao do protocolo, como texto
        produto  codigo do produto, quando o anuncio traz

    Lista vazia quer dizer "ninguem se anunciou", que nao e o mesmo que
    "nao existe": aparelho desligado da tomada, em outra rede, ou uma rede
    que bloqueia esse tipo de anuncio dao o mesmo resultado. Quem chama
    precisa dizer isso na tela em vez de afirmar que nao ha nada.

    NUNCA levanta excecao: rede indisponivel vira lista vazia. Esta funcao e
    chamada na primeira abertura do programa, e uma tela de configuracao que
    quebra e pior do que uma que nao acha.
    """
    try:
        import tinytuya
    except Exception:
        return []

    try:
        # `poll=False` de proposito: com poll, a biblioteca ainda TENTA ler o
        # estado de cada aparelho -- e isso exige a chave, que e justamente o
        # que nao se tem aqui. Sem ele, so escuta os anuncios.
        achados = tinytuya.deviceScan(
            verbose=False, maxretry=segundos, color=False, poll=False,
        )
    except Exception:
        return []

    lista = []
    for ip, dados in (achados or {}).items():
        dados = dados or {}
        lista.append({
            "id": str(dados.get("gwId") or dados.get("id") or ""),
            "ip": str(dados.get("ip") or ip or ""),
            "versao": str(dados.get("version") or ""),
            "produto": str(dados.get("productKey") or ""),
        })

    lista.sort(key=lambda a: _ordem_do_ip(a["ip"]))
    return lista


def procurar_insistindo(ciclos: int = CICLOS_PADRAO,
                        segundos: int = SEGUNDOS_PADRAO,
                        ao_tentar=None) -> list[dict]:
    """
    Escuta varias vezes seguidas e para na primeira que achar algo.

    PARA NA PRIMEIRA QUE ACHAR, e nao junta os achados de todas: quem
    respondeu apareceu, e continuar escutando so faria a tela esperar por
    aparelho que talvez nao exista. Quem tiver mais de um aparelho e a
    infelicidade de um deles se anunciar tarde clica em "Procurar de novo" --
    barato, e melhor do que vinte e quatro segundos de espera para todo mundo.

    `ao_tentar(ciclo, total)` e chamado antes de cada escuta, para quem chamou
    poder mostrar que ainda esta procurando. CUIDADO: ele roda na thread da
    varredura, entao nao pode tocar em widget -- so deixar recado.

    Nunca levanta excecao, pela mesma razao de `procurar`.
    """
    total = max(1, int(ciclos))

    for ciclo in range(1, total + 1):
        if ao_tentar is not None:
            try:
                ao_tentar(ciclo, total)
            except Exception:
                # Aviso de progresso que falha nao pode derrubar a varredura:
                # ele e enfeite, ela e a funcao.
                pass

        achados = procurar(segundos)
        if achados:
            return achados

    return []


def _ordem_do_ip(ip: str) -> tuple:
    """
    Ordena por endereco de verdade, e nao alfabeticamente.

    Em texto, "192.168.0.9" vem depois de "192.168.0.10", que e o tipo de
    lista que parece embaralhada sem motivo.
    """
    try:
        return tuple(int(parte) for parte in ip.split("."))
    except Exception:
        return (999, 999, 999, 999)


def descrever(aparelho: dict) -> str:
    """
    Uma linha curta sobre o aparelho, para a tela nao ter que formatar nada.

    O identificador vem ABREVIADO: ele tem vinte e poucos caracteres, nao
    cabe num popover de 280 px e ninguem o le inteiro -- serve para conferir
    que sao aparelhos diferentes, e para isso as ultimas seis casas bastam.
    """
    partes = []
    if aparelho.get("ip"):
        partes.append(aparelho["ip"])
    if aparelho.get("id"):
        partes.append("..." + aparelho["id"][-6:])
    return "  ".join(partes)


def compativel(aparelho: dict) -> bool:
    """O aparelho fala a versao de protocolo que este programa entende?"""
    return str(aparelho.get("versao") or "") == VERSAO_ESPERADA


if __name__ == "__main__":
    import sys
    from datetime import datetime

    from . import __version__, caminhos

    linhas = []

    def anotar(texto: str = "") -> None:
        linhas.append(texto)
        print(texto)

    anotar("LightWireless %s -- sonda de rede" % __version__)
    anotar("Rodado em %s" % datetime.now().strftime("%d/%m/%Y %H:%M:%S"))
    anotar()
    anotar("Escutando a rede por %d segundos..." % SEGUNDOS_PADRAO)
    anotar()

    encontrados = procurar()

    if not encontrados:
        anotar("NAO ACHEI NENHUM APARELHO TUYA.")
        anotar()
        anotar("Isso pode ser:")
        anotar("  - a lampada esta desligada da tomada")
        anotar("  - o computador esta em outra rede (ou no Wi-Fi de visitante)")
        anotar("  - a rede bloqueia o tipo de anuncio que estes aparelhos usam")
    else:
        anotar("ACHEI %d APARELHO(S)" % len(encontrados))
        anotar()
        for i, achado in enumerate(encontrados, 1):
            marca = "" if compativel(achado) else "   <- outra versao"
            anotar("  %d. %s" % (i, descrever(achado)))
            anotar("     versao do protocolo: %s%s"
                   % (achado["versao"] or "?", marca))
            anotar("     produto: %s" % (achado["produto"] or "(nao veio)"))
            anotar("     identificador completo: %s" % achado["id"])
            anotar()

    texto = "\n".join(linhas) + "\n"
    pasta = caminhos.pasta_do_programa() / "relatorios"
    try:
        # NOME PROPRIO, e nao o `ultimo.txt` da bancada: as duas coisas rodam
        # em momentos diferentes e uma sobrescreveria a outra. O historico
        # continua sendo um so -- ali tudo acumula em ordem, e misturar e o
        # que faz sentido.
        pasta.mkdir(parents=True, exist_ok=True)
        (pasta / "ultima-sonda.txt").write_text(texto, encoding="utf-8")
        with (pasta / "historico.txt").open("a", encoding="utf-8") as arq:
            arq.write(texto)
            arq.write("\n\n" + "=" * 70 + "\n\n")
        print("Relatorio gravado em %s" % pasta)
    except Exception as erro:
        print("Nao consegui gravar o relatorio: %s" % erro)

    sys.exit(0 if encontrados else 1)
