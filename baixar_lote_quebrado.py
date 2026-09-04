"""
Baixa em lote o "Comprovante de Situacao Cadastral no CPF" (Receita Federal)
para uma lista de CPF + data de nascimento, salvando um PDF por credor.

Entrada: CSV com as colunas  cpf;data_nascimento;nome  (cabecalho obrigatorio).
Saida:   um PDF por CPF em --saida  +  relatorio.csv com o status de cada um.

Uso:
    venv/bin/python baixar_lote.py --entrada /home/wagner/Downloads/carga_csv/cpfs_para_consulta.csv \
                                   --saida  /home/wagner/Downloads/carga_csv/comprovantes

Retoma de onde parou: CPF que ja tem PDF na pasta de saida e pulado.
"""

import argparse
import base64
import csv
import os
import random
import re
import sys
import time
import urllib.request
import warnings

warnings.filterwarnings("ignore")

import google.generativeai as genai
from playwright.sync_api import sync_playwright

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)

from shot_groq import parse_captcha_answer, perguntar_gemini  # noqa: E402

URL = "https://servicos.receita.fazenda.gov.br/Servicos/CPF/ConsultaSituacao/ConsultaPublica.asp"
CDP_URL = "http://127.0.0.1:9222"

PERGUNTA_CAPTCHA = (
    "Voce resolve hCaptcha. A imagem tem um enunciado em portugues no topo, uma imagem "
    "de REFERENCIA no canto superior direito e uma grade 3x3 numerada 1-9 (esquerda para "
    "direita, cima para baixo).\n"
    "O criterio do enunciado NEM SEMPRE e 'igual a referencia': pode ser habitat "
    "('vivem onde vive'), categoria ou contexto. Leia o enunciado antes de decidir.\n"
    "As imagens sao distorcidas de proposito - cores falsas, marcas d'agua, escurecidas. "
    "Uma celula obscurecida que atende ao criterio CONTA e deve entrar na resposta.\n"
    "Responda SOMENTE os numeros das celulas que atendem, separados por virgula. "
    "Exemplo: 2,5,8")

DEBUG_DIR = os.environ.get("LOTE_DEBUG_DIR", "")

HUMANISE = None
try:
    from movimento_humano import humanise_mouse_playwright as HUMANISE
except Exception:
    pass


# ---------------------------------------------------------------- entrada ---

def normaliza_cpf(valor):
    digitos = re.sub(r"\D", "", valor or "")
    return digitos.zfill(11) if 0 < len(digitos) <= 11 else digitos


def normaliza_data(valor):
    """Aceita dd/mm/aaaa, dd-mm-aaaa, ddmmaaaa ou aaaa-mm-dd. Devolve dd/mm/aaaa."""
    bruto = (valor or "").strip()
    if not bruto:
        return ""
    iso = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", bruto)
    if iso:
        return "%s/%s/%s" % (iso.group(3), iso.group(2), iso.group(1))
    digitos = re.sub(r"\D", "", bruto)
    if len(digitos) == 8:
        return "%s/%s/%s" % (digitos[:2], digitos[2:4], digitos[4:])
    return bruto


def ler_entrada(caminho):
    with open(caminho, newline="", encoding="utf-8-sig") as fh:
        primeira = fh.readline()
        if primeira.lower().startswith("sep="):
            inicio = fh.tell()
        else:
            fh.seek(0)
            inicio = 0
        amostra = fh.read(4096)
        fh.seek(inicio)
        try:
            dialeto = csv.Sniffer().sniff(amostra, delimiters=";,\t")
        except csv.Error:
            dialeto = csv.excel
            dialeto.delimiter = ";"
        leitor = csv.DictReader(fh, dialect=dialeto)
        campos = {(c or "").strip().lower(): c for c in (leitor.fieldnames or [])}
        col_cpf = campos.get("cpf") or campos.get("documento_credito")
        col_data = campos.get("data_nascimento") or campos.get("nascimento") or campos.get("data")
        col_nome = campos.get("nome") or campos.get("nome_credito")
        if not col_cpf:
            raise SystemExit("[-] O CSV precisa de uma coluna 'cpf'.")
        linhas, vistos = [], set()
        for reg in leitor:
            cpf = normaliza_cpf(reg.get(col_cpf, ""))
            if len(cpf) != 11 or cpf in vistos:
                continue
            vistos.add(cpf)
            linhas.append({
                "cpf": cpf,
                "data": normaliza_data(reg.get(col_data, "")) if col_data else "",
                "nome": (reg.get(col_nome, "") or "").strip() if col_nome else "",
            })
        return linhas


# ---------------------------------------------------------------- consulta ---

# A cota do free tier (GenerateRequestsPerDayPerProjectPerModel) e por modelo:
# ao esgotar um, o proximo da fila entra com 500 chamadas novas.
# Ordem testada: os "lite" respondem em ~5s e acertam; os flash novos raciocinam
# antes de responder (2min+ por chamada) e so entram como ultimo recurso.
MODELOS_PADRAO = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
                  "gemini-3.5-flash", "gemini-2.5-flash"]

# Modelo que raciocina precisa de folga de tokens ou devolve resposta vazia.
TOKENS_POR_MODELO = {"gemini-3.5-flash": 400, "gemini-2.5-flash": 400,
                     "gemini-3.6-flash": 400, "gemini-3.7-flash": 400}


class SemModelos(Exception):
    """Acabaram as combinacoes de chave x modelo disponiveis hoje."""


class Rotador(object):
    """Fala com o Gemini trocando de CHAVE primeiro e de modelo depois.

    A cota diaria do free tier e por projeto e por modelo, entao a mesma
    pergunta que esgotou numa chave volta a funcionar na outra. Como o
    3.5-flash-lite e o que mais acerta, esgotar a chave nao deve rebaixar
    o modelo: troca-se a chave e ele continua em uso.
    """

    def __init__(self, nomes, chaves):
        self.pares = [(m, k) for m in nomes for k in chaves]
        self.rotulos = ["%s/chave%d" % (m, chaves.index(k) + 1) for m, k in self.pares]
        self.i = 0
        self._model = None

    @property
    def atual(self):
        return self.rotulos[self.i] if self.i < len(self.pares) else None

    def _instancia(self):
        if self._model is None:
            modelo, chave = self.pares[self.i]
            genai.configure(api_key=chave)
            self._model = genai.GenerativeModel(
                modelo,
                generation_config={"temperature": 0,
                                   "max_output_tokens": TOKENS_POR_MODELO.get(modelo, 30)})
        return self._model

    def _proximo(self, motivo):
        gasto = self.atual
        self.i += 1
        self._model = None
        if self.atual is None:
            raise SemModelos("sem cota em todas as combinacoes: %s" % ", ".join(self.rotulos))
        print("    [!] %s indisponivel (%s). Trocando para %s." % (gasto, motivo, self.atual))

    def perguntar(self, imagem, pergunta):
        for _ in range(len(self.pares) * 2):
            try:
                return perguntar_gemini(self._instancia(), imagem, pergunta)
            except Exception as e:
                texto = str(e)
                if "503" in texto or "UNAVAILABLE" in texto:
                    self._proximo("503")
                    continue
                if "429" not in texto:
                    print("    [-] Erro Gemini (%s): %s" % (self.atual, texto[:120]))
                    return None
                if "PerDay" in texto or "per_day" in texto or "free_tier_requests" in texto:
                    self._proximo("cota diaria")
                    continue
                print("    [!] Limite por minuto em %s; aguardando 12s..." % self.atual)
                time.sleep(12)
        return None


def esperar_grade(page, limite=8.0):
    """Espera as 9 celulas do desafio terem imagem carregada.

    O sleep fixo de 0.8s fotografava a grade ainda vazia e o modelo respondia
    "1,2,3,4,5,6,7,8,9" - metade das tentativas era desperdicada assim.
    """
    fim = time.time() + limite
    while time.time() < fim:
        for frame in page.frames:
            if "hcaptcha" in frame.url and "challenge" in frame.url:
                try:
                    pronto = frame.evaluate("""() => {
                        const cels = document.querySelectorAll('.task-image');
                        if (cels.length < 9) return false;
                        return Array.from(cels).every(c => {
                            const alvo = c.querySelector('.image') || c;
                            const bg = getComputedStyle(alvo).backgroundImage;
                            return bg && bg !== 'none' && bg.includes('url');
                        });
                    }""")
                    if pronto:
                        time.sleep(0.3)  # respiro para o ultimo frame pintar
                        return True
                except Exception:
                    pass
                break
        time.sleep(0.25)
    return False


def token_ok(page):
    """True se o hCaptcha ja devolveu token valido."""
    try:
        t = page.evaluate("() => { const e = document.querySelector("
                          "\"textarea[name='h-captcha-response']\"); return e ? e.value : ''; }")
        return bool(t) and len(t) > 100
    except Exception:
        return False


def iframe_desafio(page, limite=8.0):
    """Devolve o iframe do desafio so quando ele estiver visivel e com tamanho real.

    Sem essa checagem o script caia no fallback de fotografar a pagina inteira,
    e o modelo respondia "a imagem nao contem um captcha".
    """
    fim = time.time() + limite
    while time.time() < fim:
        for frame in page.frames:
            # o titulo do iframe muda conforme o idioma; o src do frame nao
            if "hcaptcha" in frame.url and "challenge" in frame.url:
                try:
                    el = frame.frame_element()
                    if el.is_visible():
                        caixa = el.bounding_box()
                        if caixa and caixa["width"] > 100 and caixa["height"] > 100:
                            return el
                except Exception:
                    pass
        time.sleep(0.25)
    return None


def abrir_desafio(page, limite=12.0):
    """Espera o checkbox do hCaptcha existir, clica e espera o desafio abrir.

    Na rodada 2 a pagina acabou de recarregar: sem esperar o checkbox aparecer,
    o clique caia no vazio e o desafio nunca abria.
    """
    for _ in range(2):
        try:
            cb = page.locator(".h-captcha iframe").first
            cb.wait_for(state="visible", timeout=limite * 1000)
            caixa = cb.bounding_box()
        except Exception:
            caixa = None
        if not caixa:
            return None
        page.mouse.move(caixa["x"] + 32, caixa["y"] + caixa["height"] / 2, steps=8)
        time.sleep(0.2)
        page.mouse.click(caixa["x"] + 32, caixa["y"] + caixa["height"] / 2)
        # o desafio leva ~6s para montar, e mais ainda depois de uma falha;
        # com espera curta o script desistia de um captcha que ia abrir
        alvo = iframe_desafio(page, limite=25.0)
        if alvo is not None:
            return alvo
        time.sleep(2)
    return None


def resolver_captcha(page, model, tentativas=5, corte=4, tag=""):
    """Clica no hCaptcha e resolve o desafio com o Gemini. True se passou.

    Desiste na tentativa `corte` para o chamador recarregar a pagina e pegar
    um captcha novo (insistir no mesmo desafio raramente vira acerto).
    """
    tentativas = min(tentativas, corte)
    abrir_desafio(page)

    for tentativa in range(tentativas):
        if "ConsultaPublicaExibir" in page.url or token_ok(page):
            return True

        # o desafio troca de pagina e o handle antigo morre: reobter sempre,
        # inclusive depois da espera pelas imagens, logo antes do screenshot
        if iframe_desafio(page, limite=6.0) is None:
            print("    [!] Desafio nao abriu nesta rodada.")
            return False

        esperar_grade(page)
        alvo = iframe_desafio(page, limite=3.0)
        if alvo is None:
            print("    [!] Desafio sumiu antes do screenshot; reabrindo.")
            time.sleep(1)
            continue
        try:
            alvo.screenshot(path="/tmp/lote_challenge.png", timeout=5000)
        except Exception as e:
            print("    [!] Falhou fotografar o desafio (%s)." % str(e).splitlines()[0][:60])
            time.sleep(1)
            continue

        resposta = model.perguntar("/tmp/lote_challenge.png", PERGUNTA_CAPTCHA)
        if not resposta:
            continue

        numeros = parse_captcha_answer(resposta)
        crua = " ".join((resposta or "").split())[:90]
        print("    [i] Tentativa %d -> %s | resposta: %r" % (tentativa + 1, numeros or "sem numeros", crua))
        if DEBUG_DIR:
            try:
                import shutil as _sh
                _sh.copyfile("/tmp/lote_challenge.png",
                             os.path.join(DEBUG_DIR, "%s_t%d.png" % (tag or "sem", tentativa + 1)))
            except Exception:
                pass
        if not numeros:
            continue
        if len(numeros) >= 7:
            # 7+ celulas = o modelo nao entendeu o desafio; clicar tudo nunca passa
            print("    [!] Resposta degenerada (%d celulas), descartando." % len(numeros))
            continue

        for frame in page.frames:
            if "hcaptcha" in frame.url and "challenge" in frame.url:
                desafio = frame
                break

        imagens = desafio.locator(".task-image")
        total = imagens.count()
        for num in numeros:
            idx = num - 1
            if not (0 <= idx < total):
                continue
            try:
                cx_box = imagens.nth(idx).bounding_box()
                if cx_box:
                    page.mouse.move(cx_box["x"] + cx_box["width"] / 2,
                                    cx_box["y"] + cx_box["height"] / 2, steps=6)
                    time.sleep(0.1)
                    page.mouse.click(cx_box["x"] + cx_box["width"] / 2,
                                     cx_box["y"] + cx_box["height"] / 2)
                    time.sleep(0.2)
            except Exception:
                pass

        time.sleep(0.3)
        try:
            enviar = desafio.locator("button.button-submit, [role=button].button-submit, "
                                     "div.button-submit, a.button-submit")
            if enviar.count() > 0:
                enviar.first.click(timeout=3000)
        except Exception:
            pass
        time.sleep(2)

        if "ConsultaPublicaExibir" in page.url:
            return True
        token = page.evaluate(
            "() => { const e = document.querySelector(\"textarea[name='h-captcha-response']\");"
            " return e ? e.value : ''; }"
        )
        if token and len(token) > 100:
            return True
    return False


def salvar_pdf(cdp, caminho):
    resultado = cdp.send("Page.printToPDF", {
        "printBackground": True,
        "marginTop": 0.4, "marginBottom": 0.4,
        "marginLeft": 0.4, "marginRight": 0.4,
    })
    with open(caminho, "wb") as fh:
        fh.write(base64.b64decode(resultado["data"]))


def mensagem_erro(page):
    try:
        texto = page.inner_text("body")[:4000]
    except Exception:
        return ""
    for padrao in ("Data de nascimento informada", "CPF informado", "não existe",
                   "nao existe", "Erro no preenchimento", "sistema encontra-se"):
        if padrao.lower() in texto.lower():
            linha = [l.strip() for l in texto.splitlines()
                     if padrao.lower() in l.lower() and l.strip()]
            if linha:
                return linha[0][:200]
    return ""


def uma_rodada(page, cdp, model, registro, saida, tentativas, corte):
    cpf, data = registro["cpf"], registro["data"]
    destino = os.path.join(saida, "%s.pdf" % cpf)

    page.bring_to_front()
    page.goto(URL, wait_until="domcontentloaded")
    time.sleep(0.8)
    if HUMANISE:
        try:
            HUMANISE(page)
        except Exception:
            pass

    page.fill("#txtCPF", cpf)
    page.evaluate("() => document.getElementById('txtCPF').blur()")
    page.fill("#txtDataNascimento", data)
    page.evaluate("() => document.getElementById('txtDataNascimento').blur()")
    time.sleep(0.3)

    resolver_captcha(page, model, tentativas, corte, tag=cpf)

    if "ConsultaPublicaExibir" not in page.url:
        token = page.evaluate(
            "() => { const e = document.querySelector(\"textarea[name='h-captcha-response']\");"
            " return e ? e.value : ''; }"
        )
        if token:
            page.evaluate(r"""(t) => {
                var ta = document.querySelector("textarea[name='h-captcha-response']");
                if (ta) ta.value = t;
                var chk = document.getElementsByName("idCheckedReCaptcha")[0];
                if (chk) chk.value = "true";
                var a = document.getElementById("h-recaptcha-response");
                if (!a) {
                    a = document.createElement("input");
                    a.type = "hidden"; a.id = "h-recaptcha-response";
                    a.name = "h-recaptcha-response";
                    (document.getElementById("theForm") || document.body).appendChild(a);
                }
                a.value = t;
            }""", token)
        page.keyboard.press("Escape")
        time.sleep(0.3)
        try:
            page.click("#id_submit", force=True)
        except Exception:
            pass
        time.sleep(2.5)

    if "ConsultaPublicaExibir" in page.url:
        salvar_pdf(cdp, destino)
        return "ok", destino

    erro = mensagem_erro(page)
    return ("dados_invalidos" if erro else "captcha_falhou"), erro


def consultar(page, cdp, model, registro, saida, tentativas=5, corte=4, rodadas=2):
    """Tenta o CPF em ate `rodadas` passadas; cada uma recarrega a pagina e
    pega um captcha novo. Dado invalido nao rende segunda tentativa."""
    status, detalhe = "captcha_falhou", ""
    for rodada in range(1, rodadas + 1):
        status, detalhe = uma_rodada(page, cdp, model, registro, saida, tentativas, corte)
        if status == "ok" or status == "dados_invalidos":
            return status, detalhe
        if rodada < rodadas:
            print("    [i] Captcha nao saiu na rodada %d; recarregando com desafio novo." % rodada)
            time.sleep(2)
    return status, detalhe


# ------------------------------------------------------------------- main ---

def main():
    ap = argparse.ArgumentParser(description="Baixa comprovantes de situacao cadastral em lote.")
    ap.add_argument("--entrada", required=True, help="CSV com cpf;data_nascimento;nome")
    ap.add_argument("--saida", default=os.path.join(AQUI, "comprovantes"), help="pasta dos PDFs")
    ap.add_argument("--intervalo", type=float, default=12.0,
                    help="pausa media entre consultas, em segundos (padrao 12)")
    ap.add_argument("--limite", type=int, default=0, help="processa apenas os N primeiros")
    ap.add_argument("--refazer", action="store_true", help="reconsulta CPF que ja tem PDF")
    ap.add_argument("--tentativas", type=int, default=5,
                    help="teto de tentativas de captcha por rodada (padrao 5)")
    ap.add_argument("--corte", type=int, default=4,
                    help="desiste da rodada nesta tentativa e recarrega (padrao 4)")
    ap.add_argument("--modelos", default=os.environ.get("GEMINI_MODELOS", ",".join(MODELOS_PADRAO)),
                    help="modelos em ordem de uso, separados por virgula")
    ap.add_argument("--rodadas", type=int, default=2,
                    help="rodadas por CPF antes de pular (padrao 2)")
    args = ap.parse_args()

    chave = os.environ.get("GEMINI_API_KEY", "")
    if not chave:
        raise SystemExit("[-] Defina GEMINI_API_KEY (ou carregue o .env).")

    try:
        urllib.request.urlopen(CDP_URL + "/json/version", timeout=2)
    except Exception:
        raise SystemExit("[-] Chrome sem CDP. Rode antes: venv/bin/python iniciar_chrome.py")

    registros = ler_entrada(args.entrada)
    sem_data = [r for r in registros if not r["data"]]
    if sem_data:
        print("[!] %d de %d registros estao SEM data de nascimento e serao pulados."
              % (len(sem_data), len(registros)))
    fila = [r for r in registros if r["data"]]

    os.makedirs(args.saida, exist_ok=True)
    if not args.refazer:
        fila = [r for r in fila if not os.path.exists(os.path.join(args.saida, "%s.pdf" % r["cpf"]))]
    if args.limite:
        fila = fila[:args.limite]

    if not fila:
        print("[i] Nada a consultar (sem data de nascimento ou tudo ja baixado).")
        return

    modelos = [m.strip() for m in args.modelos.split(",") if m.strip()]
    chaves = [chave]
    for extra in ("GEMINI_API_KEY_2", "GEMINI_API_KEY_3", "GEMINI_API_KEY_4"):
        valor = os.environ.get(extra, "").strip()
        if valor and valor not in chaves:
            chaves.append(valor)
    model = Rotador(modelos, chaves)
    print("[+] %d chave(s) x %d modelo(s); ordem: %s"
          % (len(chaves), len(modelos), " -> ".join(model.rotulos[:4])))

    relatorio = os.path.join(args.saida, "relatorio.csv")
    novo = not os.path.exists(relatorio)
    log = open(relatorio, "a", newline="", encoding="utf-8")
    escritor = csv.writer(log, delimiter=";")
    if novo:
        escritor.writerow(["cpf", "nome", "data_nascimento", "status", "detalhe"])

    print("[+] %d CPF na fila. PDFs em: %s" % (len(fila), args.saida))
    contagem = {}

    with sync_playwright() as p:
        navegador = p.chromium.connect_over_cdp(CDP_URL)
        ctx = navegador.contexts[0]
        page = next((pg for pg in ctx.pages
                     if "servicos.receita.fazenda.gov.br" in pg.url), None)
        if page is None:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
        cdp = ctx.new_cdp_session(page)

        for i, registro in enumerate(fila, 1):
            rotulo = registro["nome"] or registro["cpf"]
            print("\n[%d/%d] %s (%s / %s)" % (i, len(fila), rotulo, registro["cpf"], registro["data"]))
            try:
                status, detalhe = consultar(page, cdp, model, registro, args.saida,
                                            args.tentativas, args.corte, args.rodadas)
            except SemModelos as e:
                print("\n[-] %s" % e)
                print("[i] Rode de novo apos o reset da cota; ele retoma daqui.")
                break
            except Exception as e:
                status, detalhe = "erro", str(e)[:200]
            contagem[status] = contagem.get(status, 0) + 1
            print("    -> %s %s" % (status, detalhe or ""))
            escritor.writerow([registro["cpf"], registro["nome"], registro["data"], status, detalhe])
            log.flush()
            if i < len(fila):
                pausa = max(3.0, random.uniform(args.intervalo * 0.7, args.intervalo * 1.3))
                time.sleep(pausa)

    log.close()
    print("\n" + "=" * 50)
    for status, qtd in sorted(contagem.items()):
        print("%-18s %d" % (status, qtd))
    print("relatorio: %s" % relatorio)


if __name__ == "__main__":
    main()
