"""
Conecta no CHROME REAL da máquina (via CDP), preenche CPF e data de
nascimento, resolve o hCaptcha automaticamente via hcaptcha-challenger
(Google Gemini), envia a consulta e espera chegar no "Comprovante de
Situação Cadastral no CPF".

Como rodar:
  1. venv/bin/python Captcha/iniciar_chrome.py   (abre SEU Chrome com CDP)
  2. venv/bin/python Captcha/shot_hcaptcha.py
"""

import asyncio
import os
import sys
import time
import urllib.request

from playwright.async_api import async_playwright
from hcaptcha_challenger.agent import AgentV, AgentConfig
from hcaptcha_challenger.models import CaptchaResponse

CPF = os.environ.get("CPF", "")
DATA_NASCIMENTO = os.environ.get("DATA_NASCIMENTO", "")
URL = "https://servicos.receita.fazenda.gov.br/Servicos/CPF/ConsultaSituacao/ConsultaPublica.asp"
CDP_URL = "http://127.0.0.1:9222"
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")


async def main():
    try:
        urllib.request.urlopen(CDP_URL + "/json/version", timeout=1)
    except Exception:
        print("[!] Chrome real não está com CDP ativo.")
        print("    Rode:  venv/bin/python Captcha/iniciar_chrome.py")
        return

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL)
        ctx = browser.contexts[0]

        page = None
        for pg in ctx.pages:
            if "servicos.receita.fazenda.gov.br" in pg.url:
                page = pg
                break
        if page is None:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        print("[+] Conectado no SEU Chrome.")
        await page.bring_to_front()
        await page.goto(URL, wait_until="domcontentloaded")
        await asyncio.sleep(2)

        print("[+] Preenchendo CPF e Data de Nascimento...")
        await page.fill("#txtCPF", CPF)
        await page.evaluate("() => document.getElementById('txtCPF').blur()")
        await page.fill("#txtDataNascimento", DATA_NASCIMENTO)
        await page.evaluate("() => document.getElementById('txtDataNascimento').blur()")
        await asyncio.sleep(1)
        await page.screenshot(path="/tmp/rf_01_preenchido.png")
        print("[shot] /tmp/rf_01_preenchido.png")

        # ===== resolve o hCaptcha via hcaptcha-challenger =====
        print("[+] Resolvendo hCaptcha via hcaptcha-challenger (Gemini)...")
        agent_config = AgentConfig(
            gemini_api_key=GEMINI_KEY,
            IMAGE_CLASSIFIER_MODEL="gemini-3.1-pro-preview",
            SPATIAL_POINT_REASONER_MODEL="gemini-3.1-pro-preview",
            CHALLENGE_CLASSIFIER_MODEL="gemini-2.5-flash",
        )
        agent = AgentV(page=page, agent_config=agent_config)

        try:
            await agent.robotic_arm.click_checkbox()
            await agent.wait_for_challenge()
        except Exception as e:
            print("[-] Erro ao clicar no checkbox:", e)
            return

        # espera o token ser preenchido
        print("[+] Aguardando o token...")
        token = ""
        for _ in range(30):
            token = await page.evaluate(
                "() => { const e = document.querySelector("
                "\"textarea[name='h-captcha-response']\"); return e ? e.value : ''; }"
            )
            if token:
                break
            await asyncio.sleep(2)

        if not token:
            print("[-] Token não obtido. Verifique se o Gemini resolveu o desafio.")
            return

        print("[+] Token obtido! (%d caracteres)" % len(token))

        # ===== injeta o token na página (caso necessário) =====
        await page.evaluate(r"""(token) => {
            var ta = document.querySelector("textarea[name='h-captcha-response']");
            if (ta) ta.value = token;
            var chk = document.getElementsByName("idCheckedReCaptcha")[0];
            if (chk) chk.value = "true";
            var alvo = document.getElementById("h-recaptcha-response");
            if (!alvo) {
                alvo = document.createElement("input");
                alvo.type = "hidden"; alvo.id = "h-recaptcha-response";
                alvo.name = "h-recaptcha-response";
                (document.getElementById("theForm") || document.body).appendChild(alvo);
            }
            alvo.value = token;
        }""", token)
        print("[+] Token injetado na página.")
        await asyncio.sleep(1)
        await page.screenshot(path="/tmp/rf_02_token_injetado.png")
        print("[shot] /tmp/rf_02_token_injetado.png")

        # ===== clica em Consultar =====
        print("[+] Clicando em Consultar...")
        try:
            async with page.expect_navigation(wait_until="domcontentloaded", timeout=30000):
                await page.click("#id_submit")
        except Exception as e:
            print("[-] navegação:", e)
        await asyncio.sleep(2)

        # ===== espera o Comprovante de Situação Cadastral =====
        print("[+] Aguardando o Comprovante de Situação Cadastral...")
        alvo_ok = False
        try:
            await page.wait_for_selector(
                "span.clTitulo3:has-text('Comprovante de Situação Cadastral no CPF')",
                timeout=30000,
            )
            alvo_ok = True
        except Exception:
            alvo_ok = False

        await page.screenshot(path="/tmp/rf_03_resultado.png", full_page=True)
        print("[shot] /tmp/rf_03_resultado.png")
        if alvo_ok:
            print("=" * 60)
            print("RESULTADO: CHEGOU NO COMPROVANTE DE SITUAÇÃO CADASTRAL")
            print("=" * 60)
        else:
            print("RESULTADO: ? Comprovante não encontrado — verifique a tela.")

        print("\n[i] Navegador ABERTO — ele NÃO fecha sozinho.")
        if sys.stdin.isatty():
            input("    Pressione Enter AQUI no terminal para fechar o navegador...")
        else:
            print("    (sem terminal interativo — saindo sem fechar o navegador)")
            return


if __name__ == "__main__":
    asyncio.run(main())