"""
A unica conversa deste programa com a nuvem: buscar a CHAVE da lampada.

POR QUE PRECISA DA NUVEM PARA ALGO LOCAL
-----------------------------------------
A lampada se anuncia na rede sem segredo nenhum -- e o que a `descoberta.py`
usa. Mas ela so ACEITA comando de quem tem a chave dela, e essa chave nunca
trafega pela rede: e atribuida pela nuvem do fabricante quando o aparelho e
pareado, e so de la se consegue ler. Nao ha caminho local (levantado em
06/set/2026); para aparelho Wi-Fi Tuya nao existe alternativa.

Entao esta e uma visita, nao uma dependencia: a chave e buscada UMA VEZ,
gravada no `config.json`, e daquele momento em diante o programa nunca mais
fala com a internet -- so com a lampada, dentro da rede.

O QUE ELE PRECISA TER
----------------------
Uma conta de desenvolvedor na Tuya, com um projeto criado e o aplicativo do
celular vinculado a ele. Dali saem duas credenciais (um identificador e um
segredo) e a regiao do data center. **Isso substitui o assistente de linha de
comando, nao a conta.** A conta continua obrigatoria, e desde 2026 o acesso
tem periodo que expira e vira assinatura -- quando isso acontece, a consulta
para de responder sem aviso, e por isso `_traduzir_erro` tem um caso proprio
para ela.

O IDENTIFICADOR DO APARELHO VEM DE GRACA
-----------------------------------------
A nuvem exige o identificador de UM aparelho da conta para liberar a lista --
e por ele que ela descobre de qual usuario se trata. No assistente de linha
de comando esse numero e digitado a mao, procurado no site. Aqui a
`descoberta.py` acabou de ler ele do anuncio na rede, entao ninguem precisa
procurar nada.

NADA AQUI LEVANTA EXCECAO
--------------------------
Toda falha volta como um recado em portugues, pronto para aparecer na tela.
Credencial trocada, regiao errada, assinatura vencida e internet fora sao
todos caminho normal nesta parte do programa.
"""

from __future__ import annotations

# As regioes que a Tuya tem, com o nome que ela usa no site. A chave e o
# codigo que a biblioteca entende; o valor e o que a pessoa vai reconhecer na
# tela, porque no site da Tuya o campo se chama "Data center".
REGIOES = {
    "us": "America (oeste)",
    "us-e": "America (leste)",
    "eu": "Europa (central)",
    "eu-w": "Europa (oeste)",
    "cn": "China",
    "in": "India",
}

# O projeto dele esta em Western America. Serve de padrao porque e o que a
# Tuya oferece primeiro para quem cria conta no Brasil.
REGIAO_PADRAO = "us"


def buscar(regiao: str, api_id: str, api_secret: str,
           id_aparelho: str) -> tuple[list[dict], str]:
    """
    Pede a lista de aparelhos da conta, com as chaves.

    Devolve `(lista, recado)`. Recado vazio quer dizer que deu certo; com
    recado, a lista vem vazia e o texto ja esta em portugues, pronto para a
    tela mostrar.

    Cada aparelho da lista e um dicionario simples:

        id       identificador
        chave    a chave local -- e o que se veio buscar
        nome     o nome que a pessoa deu no aplicativo do celular
        produto  o modelo, quando a nuvem informa

    O `id_aparelho` nao entra no resultado: ele e a credencial que libera a
    consulta, e a lista que volta traz a conta inteira.
    """
    faltando = _o_que_falta(regiao, api_id, api_secret, id_aparelho)
    if faltando:
        return [], faltando

    try:
        import tinytuya
    except Exception:
        return [], ("Falta a biblioteca que fala com a lampada. "
                    "Rode: pip install -r requirements.txt")

    try:
        nuvem = tinytuya.Cloud(
            apiRegion=regiao,
            apiKey=api_id.strip(),
            apiSecret=api_secret.strip(),
            apiDeviceID=id_aparelho.strip(),
        )
        resposta = nuvem.getdevices()
    except Exception as erro:
        return [], _traduzir_erro(str(erro))

    # A biblioteca devolve LISTA quando deu certo e DICIONARIO quando deu
    # errado -- o dicionario de erro tem a chave "Error" dentro. Conferir o
    # tipo antes de iterar evita tratar a mensagem de erro como se fosse um
    # aparelho chamado "Error".
    if isinstance(resposta, dict):
        return [], _traduzir_erro(
            "%s %s" % (resposta.get("Error", ""), resposta.get("Payload", ""))
        )

    if not isinstance(resposta, list):
        return [], "A nuvem respondeu de um jeito que eu nao esperava."

    lista = []
    for bruto in resposta:
        if not isinstance(bruto, dict):
            continue
        identificador = str(bruto.get("id") or "")
        chave = str(bruto.get("key") or "")
        if not identificador or not chave:
            # Aparelho sem chave nao serve para nada aqui, e mostrar na tela
            # um item que nao da para escolher e pior do que esconder.
            continue
        lista.append({
            "id": identificador,
            "chave": chave,
            "nome": str(bruto.get("name") or ""),
            "produto": str(bruto.get("product_name")
                           or bruto.get("product_id") or ""),
        })

    if not lista:
        return [], ("A nuvem respondeu, mas sem nenhum aparelho com chave. "
                    "Confira se o aplicativo do celular esta vinculado a este "
                    "projeto na Tuya.")

    return lista, ""


def achar(lista: list[dict], id_aparelho: str) -> dict | None:
    """O aparelho de um identificador dentro da lista que a nuvem devolveu."""
    for aparelho in lista:
        if aparelho.get("id") == id_aparelho:
            return aparelho
    return None


def _o_que_falta(regiao: str, api_id: str, api_secret: str,
                 id_aparelho: str) -> str:
    """
    Confere o que a pessoa deixou em branco, ANTES de bater na nuvem.

    Ir na internet para descobrir que um campo esta vazio custa segundos e
    devolve uma mensagem do fabricante em vez de uma frase util.
    """
    if not (api_id or "").strip():
        return "Falta o identificador do projeto (o campo Access ID na Tuya)."
    if not (api_secret or "").strip():
        return "Falta o segredo do projeto (o campo Access Secret na Tuya)."
    if not (id_aparelho or "").strip():
        return ("Falta o identificador de um aparelho da conta -- normalmente "
                "ele vem da varredura da rede.")
    if regiao not in REGIOES:
        return "Regiao desconhecida: %s" % regiao
    return ""


def _traduzir_erro(bruto: str) -> str:
    """
    A resposta do fabricante virada frase util em portugues.

    ELE NAO LE MENSAGEM DE ERRO DE MAQUINA, e a da Tuya e pior do que a
    media: vem em ingles, com codigo numerico e sem dizer o que fazer. Cada
    caso aqui e um problema real com um proximo passo -- o texto cru fica
    para o registro, que so eu leio.
    """
    texto = (bruto or "").lower()

    # A assinatura vencida e o caso mais provavel e o mais confuso: a conta
    # existe, as credenciais estao certas, e a consulta simplesmente para.
    if ("28841002" in texto or "no permissions" in texto
            or "subscription" in texto or "expired" in texto):
        return ("O acesso de desenvolvedor da Tuya expirou. Entre no site "
                "deles, em Cloud Services, e renove o periodo de teste do "
                "IoT Core -- a chave que voce ja tem continua funcionando.")

    if ("sign invalid" in texto or "1004" in texto
            or "secret" in texto or "invalid client" in texto):
        return ("O identificador ou o segredo nao foram aceitos. Confira se "
                "copiou os dois inteiros, sem espaco sobrando.")

    if "1106" in texto or "permission" in texto:
        return ("A conta nao tem permissao para ler este aparelho. Confira se "
                "o aplicativo do celular esta vinculado a este projeto na "
                "Tuya.")

    if ("cross-region" in texto or "not exists" in texto
            or "device not found" in texto):
        return ("A nuvem nao achou este aparelho nesta regiao. Confira em qual "
                "data center o seu projeto foi criado e escolha essa regiao.")

    if ("timed out" in texto or "timeout" in texto or "connection" in texto
            or "resolve" in texto or "network" in texto or "ssl" in texto):
        return ("Nao consegui falar com a nuvem da Tuya. Confira a internet e "
                "tente de novo.")

    if not texto.strip():
        return "A nuvem recusou a consulta e nao disse por que."

    # Nao reconhecido: o texto cru vai junto, porque sem ele eu nao teria como
    # descobrir depois qual caso novo apareceu.
    return "A nuvem recusou a consulta: %s" % bruto


if __name__ == "__main__":
    import json
    import sys
    from datetime import datetime

    from . import __version__, caminhos, descoberta

    linhas = []

    def anotar(texto: str = "") -> None:
        linhas.append(texto)
        print(texto)

    def perguntar(rotulo: str) -> str:
        try:
            return input(rotulo).strip()
        except (EOFError, KeyboardInterrupt):
            return ""

    anotar("LightWireless %s -- sonda da nuvem" % __version__)
    anotar("Rodado em %s" % datetime.now().strftime("%d/%m/%Y %H:%M:%S"))
    anotar()

    # As credenciais saem do `tinytuya.json` que o assistente de linha de
    # comando deixa na pasta, quando ele ja rodou aqui. E o caminho sem
    # digitacao; sem o arquivo, pergunta.
    credenciais = {}
    arquivo = caminhos.arquivo("tinytuya.json")
    if arquivo.exists():
        try:
            credenciais = json.loads(arquivo.read_text(encoding="utf-8")) or {}
            anotar("Credenciais lidas do tinytuya.json que ja estava aqui.")
        except Exception:
            credenciais = {}
            anotar("O tinytuya.json esta ilegivel; vou perguntar.")

    regiao = str(credenciais.get("apiRegion") or "") or REGIAO_PADRAO
    api_id = str(credenciais.get("apiKey") or "")
    api_secret = str(credenciais.get("apiSecret") or "")

    if not api_id:
        api_id = perguntar("Access ID do projeto na Tuya: ")
    if not api_secret:
        api_secret = perguntar("Access Secret do projeto: ")

    anotar()
    anotar("Regiao: %s (%s)" % (regiao, REGIOES.get(regiao, "?")))
    anotar()

    # O identificador do aparelho sai da rede, nao da digitacao -- e o ponto
    # que faz esta tela valer mais do que o assistente.
    anotar("Procurando um aparelho na rede para liberar a consulta...")
    achados = descoberta.procurar()

    # O primeiro achado COM identificador, e nao simplesmente o primeiro: a
    # varredura aceita anuncio sem identificador, e um id vazio aqui viraria
    # "falta o identificador do aparelho" -- recado certo pelo motivo errado.
    servem = [a for a in achados if a.get("id")]

    if servem:
        id_aparelho = servem[0]["id"]
        anotar("  achei: %s" % descoberta.descrever(servem[0]))
    else:
        anotar("  nao achei nenhum aparelho na rede.")
        id_aparelho = perguntar("Identificador de um aparelho da conta: ")

    anotar()
    anotar("Consultando a nuvem...")
    anotar()

    aparelhos, recado = buscar(regiao, api_id, api_secret, id_aparelho)

    if recado:
        anotar("NAO DEU:")
        anotar("  %s" % recado)
    else:
        anotar("A NUVEM DEVOLVEU %d APARELHO(S)" % len(aparelhos))
        anotar()
        for i, aparelho in enumerate(aparelhos, 1):
            daqui = " <- este e o da rede" if aparelho["id"] == id_aparelho else ""
            anotar("  %d. %s%s" % (i, aparelho["nome"] or "(sem nome)", daqui))
            anotar("     identificador: %s" % aparelho["id"])
            anotar("     produto: %s" % (aparelho["produto"] or "(nao veio)"))
            # A CHAVE NAO VAI PARA O RELATORIO. Ele e gravado em disco e eu o
            # leio pelo fmcp; a chave nao tem por que passar por nenhum dos
            # dois. Basta saber que ela veio.
            anotar("     chave: recebida (%d caracteres)"
                   % len(aparelho["chave"]))
            anotar()

    texto = "\n".join(linhas) + "\n"
    pasta = caminhos.pasta_do_programa() / "relatorios"
    try:
        pasta.mkdir(parents=True, exist_ok=True)
        (pasta / "ultima-nuvem.txt").write_text(texto, encoding="utf-8")
        with (pasta / "historico.txt").open("a", encoding="utf-8") as arq:
            arq.write(texto)
            arq.write("\n\n" + "=" * 70 + "\n\n")
        print("Relatorio gravado em %s" % pasta)
    except Exception as erro:
        print("Nao consegui gravar o relatorio: %s" % erro)

    sys.exit(0 if aparelhos else 1)
