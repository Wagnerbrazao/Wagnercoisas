"""Sugere GPREV ou INSS por CPF: venv/bin/python gprev.py 70508973104.

Usa LOGIN_GPREV e PASSWORD_GPREV do ambiente ou .env. Apenas consulta:
nunca inclui registros nem emite DARE. O captcha é lido localmente.
Consulta com dados sugere GPREV; consulta vazia sugere INSS.
"""

import argparse
import collections
import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from PIL import Image, ImageFilter, ImageOps
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

AQUI = Path(__file__).resolve().parent
URL = "https://sistemas.goiasprev.go.gov.br/"
FUSO = ZoneInfo("America/Sao_Paulo")


def normalizar_cpf(valor):
    cpf = re.sub(r"[.\-\s]", "", valor)
    if not re.fullmatch(r"\d{11}", cpf) or len(set(cpf)) == 1:
        raise ValueError("Informe um CPF válido com 11 dígitos.")
    for tamanho in (9, 10):
        soma = sum(int(n) * peso for n, peso in zip(cpf[:tamanho], range(tamanho + 1, 1, -1)))
        if int(cpf[tamanho]) != (soma * 10 % 11) % 10:
            raise ValueError("O CPF informado tem dígitos verificadores inválidos.")
    return cpf


def sem_acentos(texto):
    return "".join(c for c in unicodedata.normalize("NFD", texto.lower())
                   if unicodedata.category(c) != "Mn")


def tempo_restante(prazo, limite=30):
    restante = prazo - time.monotonic()
    if restante <= 0:
        raise TimeoutError("Tempo máximo da consulta atingido.")
    return max(1, int(min(restante, limite) * 1000))


def ocultar_credenciais(texto):
    for chave in ("PASSWORD_GPREV", "LOGIN_GPREV"):
        segredo = os.environ.get(chave, "")
        if segredo:
            texto = texto.replace(segredo, "[credencial]")
    return texto


def ler_captcha(imagem, prazo):
    """Remove traços finos e compara leituras do Tesseract, sem serviços externos."""
    original = Image.open(io.BytesIO(imagem)).convert("L")
    original = original.crop((3, 3, original.width - 3, original.height - 3))
    candidatos = collections.defaultdict(list)
    for limiar in (100, 130, 80):
        binaria = original.point(lambda pixel: 255 if pixel > limiar else 0)
        for filtro in (5, 3):
            mascara = binaria.filter(ImageFilter.MaxFilter(filtro))
            caixa = ImageOps.invert(mascara).getbbox()
            if caixa is None:
                continue
            tratada = mascara.filter(ImageFilter.MinFilter(filtro)).crop(caixa)
            tratada = ImageOps.expand(tratada.resize((tratada.width * 3, tratada.height * 3)),
                                     border=25, fill=255)
            buffer = io.BytesIO()
            tratada.save(buffer, format="PNG")
            for modo in (7, 8):
                resposta = subprocess.run(
                    ["tesseract", "stdin", "stdout", "--psm", str(modo), "-c",
                     "tessedit_char_whitelist=abcdefghijklmnopqrstuvwxyz0123456789", "tsv"],
                    input=buffer.getvalue(), capture_output=True, check=True,
                    timeout=tempo_restante(prazo, 8) / 1000,
                )
                palavras = [linha for linha in csv.DictReader(
                    io.StringIO(resposta.stdout.decode()), delimiter="\t") if linha.get("text", "").strip()]
                codigo = "".join(p["text"].strip() for p in palavras)
                if re.fullmatch(r"[A-Za-z0-9]{5}", codigo):
                    confianca = sum(float(p["conf"]) for p in palavras) / len(palavras)
                    candidatos[codigo].append(confianca)
    if not candidatos:
        raise RuntimeError("O leitor local não conseguiu reconhecer os cinco caracteres do captcha.")
    return max(candidatos, key=lambda codigo: (len(candidatos[codigo]), max(candidatos[codigo])))


def entrar(page, prazo, tentativas, saida):
    avisos = []

    def aceitar_aviso(dialog):
        avisos.append(dialog.message)
        dialog.accept()

    page.on("dialog", aceitar_aviso)
    for tentativa in range(1, tentativas + 1):
        avisos.clear()
        page.goto(URL, wait_until="domcontentloaded", timeout=tempo_restante(prazo))
        page.locator("#login").fill(os.environ["LOGIN_GPREV"], timeout=tempo_restante(prazo))
        page.locator("#senha").fill(os.environ["PASSWORD_GPREV"], timeout=tempo_restante(prazo))
        imagem = page.locator("#frmLogin img").screenshot(timeout=tempo_restante(prazo))
        (saida / ("captcha-%d.png" % tentativa)).write_bytes(imagem)
        try:
            codigo = ler_captcha(imagem, prazo)
        except RuntimeError:
            if tentativa == tentativas:
                raise
            print("[!] Captcha ilegível; lendo uma nova imagem.", flush=True)
            continue
        page.locator("#campoKaptcha").fill(codigo, timeout=tempo_restante(prazo))
        print("[i] Login: tentativa %d/%d." % (tentativa, tentativas), flush=True)
        try:
            with page.expect_navigation(wait_until="domcontentloaded", timeout=tempo_restante(prazo, 25)):
                page.get_by_role("button", name="Entrar", exact=True).click(timeout=tempo_restante(prazo))
        except PlaywrightTimeout:
            if not page.locator("#login").count():
                raise
        if not page.locator("#login").count():
            page.get_by_role("link", name=re.compile("ARRECADAÇÃO")).wait_for(
                state="visible", timeout=tempo_restante(prazo))
            print("[+] Login confirmado.", flush=True)
            return
        mensagem = " ".join(avisos) or page.locator("body").inner_text(timeout=tempo_restante(prazo))
        normalizada = sem_acentos(mensagem)
        erro_captcha = any(p in normalizada for p in ("captcha", "codigo", "caracteres"))
        erro_credencial = any(p in normalizada for p in (
            "senha invalida", "senha incorreta", "login invalido", "login ou senha", "usuario ou senha", "bloquead"))
        if erro_captcha and not erro_credencial and tentativa < tentativas:
            print("[!] Captcha rejeitado; será lida uma nova imagem.", flush=True)
            continue
        raise RuntimeError("Login recusado pelo GPREV: " + ocultar_credenciais(mensagem.strip()))
    raise RuntimeError("O GPREV não confirmou o login.")


def abrir_cadastro(page, prazo):
    page.get_by_role("link", name=re.compile("ARRECADAÇÃO")).click(timeout=tempo_restante(prazo))
    page.get_by_role("link", name="CADASTROS", exact=True).hover(timeout=tempo_restante(prazo))
    page.get_by_role("link", name="Guias »", exact=True).hover(timeout=tempo_restante(prazo))
    page.get_by_role("link", name="Precatório", exact=True).click(timeout=tempo_restante(prazo))
    page.locator('input[name="cpf"]').wait_for(state="visible", timeout=tempo_restante(prazo))


def extrair_resultado(popup, cpf):
    parametros = parse_qs(urlparse(popup.url).query)
    if normalizar_cpf(parametros.get("cpf", [""])[0]) != cpf:
        raise RuntimeError("A janela de consulta não corresponde ao CPF solicitado.")
    dados = popup.evaluate("""() => {
        const linhas = Array.from(document.querySelectorAll('tr'));
        const cabecalho = linhas.find(tr => tr.cells.length >= 5 &&
            tr.cells[0].innerText.trim() === 'Entidade de Origem' &&
            tr.cells[1].innerText.trim() === 'Cargo');
        if (!cabecalho) return null;
        const irmas = Array.from(cabecalho.parentElement.children);
        const vinculos = irmas.slice(irmas.indexOf(cabecalho) + 1)
            .filter(tr => tr.tagName === 'TR' && tr.cells.length >= 5)
            .map(tr => Array.from(tr.cells).slice(0, 5).map(td => td.innerText.trim()))
            .filter(celulas => celulas.some(Boolean))
            .map(celulas => Object.fromEntries(
                ['entidade_origem', 'cargo', 'data_posse', 'situacao_previdenciaria',
                 'instituidor_pensao'].map((chave, i) => [chave, celulas[i]])));
        const valor = rotulo => {
            const celula = Array.from(document.querySelectorAll('td')).find(td =>
                td.querySelectorAll('table').length === 0 && td.innerText.trim().startsWith(rotulo));
            return celula ? celula.innerText.trim().slice(rotulo.length).trim() : '';
        };
        return {nome: valor('Nome do Participante:'),
                nascimento: valor('Data de Nascimento:'),
                cpf_retornado: valor('CPF:'), vinculos};
    }""")
    if dados is None:
        raise RuntimeError("A janela não exibiu a tabela esperada de vínculos previdenciários.")
    if normalizar_cpf(dados.pop("cpf_retornado")) != cpf:
        raise RuntimeError("O CPF exibido no resultado difere do CPF solicitado.")
    tem_dados = bool(dados["nome"] or dados["nascimento"] or dados["vinculos"])
    dados.update({
        "cpf": cpf,
        "status": "SUCESSO",
        "sugestao": "GPREV" if tem_dados else "INSS",
        "sub_tipo_sugerido": 11 if tem_dados else 12,
        "consulta_executada": True,
        "url": popup.url,
        "mensagem": "Sugestão GPREV: a consulta retornou dados para o CPF." if tem_dados else (
            "Sugestão INSS: a consulta ao GPREV foi concluída e retornou vazia para o CPF."),
    })
    return dados


def consultar(page, cpf, prazo):
    campo = page.locator('input[name="cpf"]')
    campo.fill("", timeout=tempo_restante(prazo))
    campo.press_sequentially(cpf, delay=40, timeout=tempo_restante(prazo))
    campo.press("Tab", timeout=tempo_restante(prazo))
    preenchido = campo.input_value(timeout=tempo_restante(prazo))
    if normalizar_cpf(preenchido) != cpf or len(preenchido) != 14:
        raise RuntimeError("O campo CPF não foi formatado corretamente pelo site.")
    with page.expect_popup(timeout=tempo_restante(prazo)) as nova_janela:
        page.get_by_role("link", name="CPF", exact=True).click(timeout=tempo_restante(prazo))
    popup = nova_janela.value
    popup.wait_for_load_state("domcontentloaded", timeout=tempo_restante(prazo))
    return popup, extrair_resultado(popup, cpf)


def main(argv=None):
    load_dotenv(AQUI / ".env")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("cpf", help="CPF a consultar, com ou sem pontuação")
    parser.add_argument("--headless", action="store_true", help="Executa sem janela visível")
    parser.add_argument("--tempo-max", type=int, default=180, help="Limite em segundos (padrão: 180)")
    parser.add_argument("--tentativas-captcha", type=int, default=3, help="Máximo de tentativas de captcha (padrão: 3)")
    parser.add_argument("--saida", type=Path, default=AQUI / "output" / "playwright" / "gprev")
    args = parser.parse_args(argv)
    try:
        cpf = normalizar_cpf(args.cpf)
    except ValueError as erro:
        parser.error(str(erro))
    if args.tempo_max <= 0 or not 1 <= args.tentativas_captcha <= 5:
        parser.error("Use tempo máximo positivo e entre 1 e 5 tentativas de captcha.")
    for chave in ("LOGIN_GPREV", "PASSWORD_GPREV"):
        if not os.environ.get(chave, "").strip():
            parser.error("Configure %s no ambiente ou no .env." % chave)
    if not shutil.which("tesseract"):
        parser.error("Instale o leitor local: sudo apt install tesseract-ocr")
    inicio = datetime.now(FUSO)
    saida = args.saida.expanduser().resolve() / (inicio.strftime("%Y%m%d-%H%M%S-%f") + "-" + cpf)
    saida.mkdir(parents=True)
    prazo = time.monotonic() + args.tempo_max
    resultado = {"cpf": cpf, "status": "ERRO", "sugestao": None,
                 "sub_tipo_sugerido": None, "consulta_executada": False}
    try:
        with sync_playwright() as p:
            com_tela = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
            navegador = p.chromium.launch(channel="chrome", headless=args.headless or not com_tela,
                                          timeout=tempo_restante(prazo))
            try:
                contexto = navegador.new_context(locale="pt-BR", viewport={"width": 1280, "height": 900})
                contexto.set_default_timeout(tempo_restante(prazo))
                page = contexto.new_page()
                entrar(page, prazo, args.tentativas_captcha, saida)
                abrir_cadastro(page, prazo)
                print("[i] Consultando CPF %s." % cpf, flush=True)
                popup, resultado = consultar(page, cpf, prazo)
                popup.screenshot(path=str(saida / "consulta.png"), full_page=True,
                                 timeout=tempo_restante(prazo))
                page.screenshot(path=str(saida / "formulario.png"), full_page=True,
                                timeout=tempo_restante(prazo))
            finally:
                navegador.close()
    except Exception as erro:
        resultado.update(status="ERRO", sugestao=None, sub_tipo_sugerido=None,
                         mensagem=ocultar_credenciais(str(erro)))
    resultado["executado_em"] = inicio.isoformat()
    (saida / "resultado.json").write_text(json.dumps(resultado, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("[%s] %s" % (resultado["status"], resultado["mensagem"]), flush=True)
    print("Resultado: %s" % (saida / "resultado.json"), flush=True)
    return 0 if resultado["status"] == "SUCESSO" else 1


if __name__ == "__main__":
    sys.exit(main())
