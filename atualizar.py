#!/usr/bin/env python3
"""Atualiza data/tabelas.json lendo os meses de referência publicados na internet.

Camadas (a de cima vence a de baixo):
  1. Páginas oficiais com extrator próprio (ex.: GOINFRA).
  2. Tabela consolidada do i9 Orçamentos (agregador, cobre ~20 bases).
Regras de segurança:
  - nunca volta para um mês anterior ao que já está no arquivo;
  - se uma fonte falhar, mantém o valor antigo e registra o erro em meta.fontes;
  - não marca nada como "confirmado": isso é ação humana no app.
"""
import datetime as dt
import json
import pathlib
import re
import sys
import urllib.request
from html.parser import HTMLParser

   ROOT = pathlib.Path(__file__).resolve().parent
   DATA = ROOT / "tabelas.json"
UA = "Mozilla/5.0 (compatible; TabelasReferenciaBot/1.0)"

MES_ABREV = {"jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
             "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12}
MES_EXT = {"JANEIRO": 1, "FEVEREIRO": 2, "MARCO": 3, "MARÇO": 3, "ABRIL": 4, "MAIO": 5,
           "JUNHO": 6, "JULHO": 7, "AGOSTO": 8, "SETEMBRO": 9, "OUTUBRO": 10,
           "NOVEMBRO": 11, "DEZEMBRO": 12}

I9_URL = "https://www.i9orcamentos.com.br/tabelas-de-precos/"
# nome da base no i9 (minúsculo) -> id no tabelas.json
I9_MAP = {
    "sinapi": "sinapi", "sicro": "sicro", "orse": "orse", "obras-sp": "obrassp",
    "sco-rio": "scorio", "emop-rj": "emop",
    "der-es edificações": "derESedif", "educação-sp": "educsp", "sicor-mg": "sicor",
    "cptm": "sptm", "sudecap-bh": "sudecap", "sedop-pa": "sedop", "caesb-df": "caesb",
    "siurb-sp": "siurb", "der-pr": "derPR", "embasa-ba": "embasa", "secid-pr": "secid",
    "saneago-go": "saneago", "setop / seinfra-mg": "setop", "seinfra-ce": "seinfraCE",
    "smop curitiba": "smop",
}
# páginas oficiais: id -> (url, extrator)
OFICIAIS = {
    "goinfraServ": ("https://www.goinfra.go.gov.br/custo-referencial-de-servicos/89", "mes_extenso"),
    "goinfraComp": ("https://www.goinfra.go.gov.br/tabela-de-composicao/114", "mes_extenso"),
}


def hoje():
    return dt.date.today()


def baixar(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "pt-BR"})
    with urllib.request.urlopen(req, timeout=45) as r:
        raw = r.read()
    return raw.decode("utf-8", errors="replace")


class Linhas(HTMLParser):
    """Extrai as linhas <tr> de todas as tabelas como listas de textos."""
    def __init__(self):
        super().__init__()
        self.linhas, self._cel, self._lin = [], None, None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._lin = []
        elif tag in ("td", "th") and self._lin is not None:
            self._cel = []

    def handle_data(self, data):
        if self._cel is not None:
            self._cel.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cel is not None and self._lin is not None:
            self._lin.append(" ".join("".join(self._cel).split()))
            self._cel = None
        elif tag == "tr" and self._lin is not None:
            if self._lin:
                self.linhas.append(self._lin)
            self._lin = None


def ym(mes, ano):
    return f"{int(ano):04d}-{int(mes):02d}"


def limite_futuro():
    h = hoje()
    m = h.month + 1
    a = h.year + (1 if m > 12 else 0)
    return ym(m if m <= 12 else 1, a)


def parse_i9(html):
    """Retorna {id: (ref 'AAAA-MM', liberada 'DD/MM/AAAA')}."""
    p = Linhas()
    p.feed(html)
    out = {}
    for lin in p.linhas:
        if len(lin) < 3:
            continue
        nome = lin[0].strip().lower()
        m = re.fullmatch(r"([a-zç]{3})/(\d{4})", lin[1].strip().lower())
        if not m or m.group(1) not in MES_ABREV:
            continue
        ident = I9_MAP.get(nome)
        if ident:
            out[ident] = (ym(MES_ABREV[m.group(1)], m.group(2)), lin[2].strip())
    return out


def parse_mes_extenso(html):
    """Acha 'ABRIL/2026', 'Abril - 2026' etc. e devolve o mês mais recente (sem datas futuras)."""
    texto = re.sub(r"<[^>]+>", " ", html)
    achados = []
    for nome, ano in re.findall(r"([A-Za-zÇç]{4,9})\s*[/\-]\s*(20\d\d)", texto):
        n = MES_EXT.get(nome.upper())
        if n:
            achados.append(ym(n, ano))
    achados = [a for a in achados if a <= limite_futuro()]
    return max(achados) if achados else None


EXTRATORES = {"mes_extenso": parse_mes_extenso}


def atualizar_item(item, ref, fonte, quando):
    """Só avança: aplica se ref for mais nova que a atual (ou se a atual estiver vazia)."""
    atual = item.get("ref", "")
    if ref and ref > atual:
        item["ref"] = ref
        item["atualizado"] = quando.isoformat()
        item["fonte"] = fonte
        return True
    return False


def main():
    dados = json.loads(DATA.read_text(encoding="utf-8"))
    itens = {x["id"]: x for x in dados["itens"]}
    fontes = {}
    mudou = []
    agora = dt.datetime.now(dt.timezone.utc)
    quando = hoje()
    d = quando.strftime("%d/%m/%Y")

    # camada 2 primeiro (i9); a camada 1 (oficial) roda depois e pode sobrescrever
    try:
        mapa = parse_i9(baixar(I9_URL))
        if not mapa:
            raise ValueError("tabela do i9 não encontrada na página")
        for ident, (ref, liberada) in mapa.items():
            if ident in itens:
                if atualizar_item(itens[ident], ref,
                                  f"i9 Orçamentos (agregador), liberada em {liberada}; lido automaticamente em {d}", quando):
                    mudou.append(f"{ident} -> {ref} (i9)")
        fontes["i9"] = {"ok": True, "bases_lidas": len(mapa)}
    except Exception as e:  # noqa: BLE001
        fontes["i9"] = {"ok": False, "erro": str(e)[:200]}

    for ident, (url, tipo) in OFICIAIS.items():
        try:
            ref = EXTRATORES[tipo](baixar(url))
            if not ref:
                raise ValueError("nenhum mês encontrado na página")
            if ident in itens and atualizar_item(itens[ident], ref,
                                                 f"Site oficial, lido automaticamente em {d}", quando):
                mudou.append(f"{ident} -> {ref} (oficial)")
            fontes[ident] = {"ok": True, "ref": ref}
        except Exception as e:  # noqa: BLE001
            fontes[ident] = {"ok": False, "erro": str(e)[:200]}

    dados["itens"] = list(itens.values())
    dados["meta"]["ultima_execucao"] = agora.strftime("%Y-%m-%dT%H:%M:%SZ")
    dados["meta"]["fontes"] = fontes
    DATA.write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding="utf-8")

    print("Alterações:", mudou or "nenhuma")
    for k, v in fontes.items():
        print(f"  {k}: {'ok' if v['ok'] else 'FALHOU - ' + v['erro']}")
    if not any(v["ok"] for v in fontes.values()):
        sys.exit(1)  # tudo falhou: deixa o workflow ficar vermelho


if __name__ == "__main__":
    main()
