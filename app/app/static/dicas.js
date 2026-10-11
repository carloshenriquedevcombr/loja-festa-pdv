// Dicas ao parar o mouse (ou focar com o teclado) em botões, campos e menus.
// De onde vem o texto: data-dica no elemento; ou a dica cinza do rótulo do campo; ou o significado do botão.
(function () {
  var BOTOES = [
    [/^✓?\s*SIM,? GUARDAR/i, "Grava de verdade. Confira os valores antes: depois só dá para desfazer com estorno."],
    [/CORRIGIR/i, "Volta para o formulário com tudo preenchido, sem gravar nada."],
    [/CONTINUAR/i, "Confere o que foi digitado e mostra um resumo antes de gravar."],
    [/IMPRIMIR/i, "Abre a impressão. Escolha a impressora e o tamanho do papel na janela do navegador."],
    [/PROCURAR|MOSTRAR/i, "Filtra a lista com o que foi digitado."],
    [/CADASTRAR|INCLUIR|PUBLICAR/i, "Cria o cadastro novo com os dados preenchidos."],
    [/SALVAR|GUARDAR/i, "Grava as mudanças desta tela."],
    [/CANCELAR/i, "Cancela. Fica registrado quem cancelou e o motivo; nada é apagado."],
    [/DESFAZER/i, "Faz o lançamento inverso (estorno). O original continua no histórico."],
    [/WHATSAPP/i, "Abre ou envia a mensagem pelo WhatsApp da loja."],
    [/RECEBER|RECEBI/i, "Registra um pagamento recebido no caixa."],
    [/PAGAR|PAGUEI/i, "Registra um pagamento feito pela loja."]
  ];
  // Explicação de cada campo pelo nome dele (vale em todas as telas)
  var CAMPOS = {
    valor: "Valor em reais, com vírgula nos centavos. Ex.: 150,00",
    forma: "Como o dinheiro entrou ou saiu. Cartão no balcão pede a maquininha; carnê e crediário pedem as parcelas.",
    maquininha: "A maquininha em que o cartão foi passado (para bater com o extrato da operadora).",
    nsu: "Número impresso no comprovante da maquininha. Ajuda a achar o pagamento no extrato.",
    parcelas: "Em quantas vezes no cartão. O juro do parcelamento segue as Configurações.",
    vezes: "Em quantas parcelas no carnê ou no crediário da loja.",
    primeiro_vencimento: "Dia em que vence a 1ª parcela. As outras vencem no mesmo dia dos meses seguintes.",
    descricao: "Escreva do jeito que você vai procurar depois (ex.: conta de luz de setembro).",
    documento: "Número da nota fiscal, do boleto ou do recibo, para achar o papel depois.",
    fornecedor: "Para quem foi o pagamento. Cadastre fornecedores em Fornecedores.",
    categoria: "Tipo de gasto: separa as despesas no relatório do mês.",
    nota: "Foto da nota ou do comprovante. Fica guardada só para a equipe do caixa.",
    cliente_nome: "Nome completo da cliente (sai no contrato e nos avisos).",
    cliente_telefone: "WhatsApp com DDD: a cliente recebe o pedido, lembretes e o link de pagamento.",
    telefone: "WhatsApp com DDD. Ex.: (19) 99999-9999",
    cpf: "CPF só com números. Obrigatório para contrato e crediário.",
    email: "E-mail para receber o comprovante e o contrato.",
    data_retirada: "Dia em que a cliente leva a peça pronta.",
    data_devolucao: "Dia em que a cliente devolve a peça alugada. Passou disso, vira atraso.",
    data_festa: "Dia da festa (ajuda a marcar provas e a conferir prazos).",
    valor_ajuste: "Quanto custa o ajuste da costureira nesta venda.",
    motivo: "Por que está fazendo isso. Fica registrado com o seu nome.",
    q: "Digite parte do nome, telefone, CPF ou código.",
    observacoes: "Anotações internas: só a equipe vê."
  };
  var caixa = null, alvo = null, timer = null;

  function textoDe(el) {
    if (el.hasAttribute("data-dica")) { return el.getAttribute("data-dica"); }
    if (el.matches("input, select, textarea")) {
      var nome = (el.getAttribute("name") || "").replace(/^c-/, "");
      var rot = el.id ? document.querySelector('label[for="' + el.id + '"]') : null;
      var d = rot && rot.querySelector(".dica, .dica-site");
      var extra = d ? d.textContent.trim() : "";
      var base = CAMPOS[nome] || "";
      if (base && extra && base.indexOf(extra) < 0) { return base + " (" + extra + ")"; }
      if (base || extra) { return base || extra; }
    }
    if (el.matches("button, a.botao, .botao")) {
      var t = (el.textContent || "").trim();
      for (var i = 0; i < BOTOES.length; i++) { if (BOTOES[i][0].test(t)) { return BOTOES[i][1]; } }
    }
    return "";
  }
  function criar() {
    caixa = document.createElement("div");
    caixa.className = "dica-flutuante";
    caixa.setAttribute("role", "tooltip");
    caixa.id = "dica-flutuante";
    document.body.appendChild(caixa);
  }
  function mostrar(el) {
    var texto = textoDe(el);
    if (!texto) { return; }
    if (!caixa) { criar(); }
    caixa.textContent = texto;
    caixa.classList.add("visivel");
    el.setAttribute("aria-describedby", "dica-flutuante");
    var r = el.getBoundingClientRect(), w = caixa.offsetWidth, h = caixa.offsetHeight;
    var menu = el.closest(".lateral");
    var x = menu ? r.right + 10 : Math.min(Math.max(8, r.left + r.width / 2 - w / 2), window.innerWidth - w - 8);
    var y = menu ? r.top + r.height / 2 - h / 2 : (r.bottom + h + 12 < window.innerHeight ? r.bottom + 8 : r.top - h - 8);
    caixa.style.left = Math.max(8, x) + "px";
    caixa.style.top = Math.max(8, y) + "px";
  }
  function esconder() {
    clearTimeout(timer);
    if (caixa) { caixa.classList.remove("visivel"); }
    if (alvo) { alvo.removeAttribute("aria-describedby"); }
    alvo = null;
  }
  var SELETOR = "[data-dica], input, select, textarea, button, a.botao";
  document.addEventListener("mouseover", function (ev) {
    var el = ev.target.closest && ev.target.closest(SELETOR);
    if (el === alvo) { return; }
    esconder();
    if (!el) { return; }
    alvo = el;
    timer = setTimeout(function () { mostrar(el); }, 450);
  });
  document.addEventListener("focusin", function (ev) {
    var el = ev.target.closest && ev.target.closest(SELETOR);
    esconder();
    if (el && el.matches(":focus-visible")) { alvo = el; mostrar(el); }
  });
  document.addEventListener("focusout", esconder);
  document.addEventListener("scroll", function () {        // rolou: some a dica aberta (a que ia abrir continua)
    if (caixa && caixa.classList.contains("visivel")) { caixa.classList.remove("visivel"); }
  }, true);
  document.addEventListener("keydown", function (ev) { if (ev.key === "Escape") { esconder(); } });
})();
