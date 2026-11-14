(function () {
  "use strict";

  var elemento = document.getElementById("grafico-preco");
  var fonte = document.getElementById("dados-grafico");
  if (!elemento || !fonte || typeof LightweightCharts === "undefined") {
    return;
  }

  var dados;
  try {
    dados = JSON.parse(fonte.textContent || "{}");
  } catch (erro) {
    return;
  }

  // Tema efetivo: a escolha salva na chave vence o sistema.
  var raiz = document.documentElement;
  var escuro = raiz.dataset.tema
    ? raiz.dataset.tema === "escuro"
    : Boolean(
        window.matchMedia &&
          window.matchMedia("(prefers-color-scheme: dark)").matches
      );
  var corTexto = escuro ? "#e9eef4" : "#17202a";
  var corGrade = escuro ? "#3d4b59" : "#c9d2db";
  var largura = elemento.clientWidth || 720;
  var altura = elemento.clientHeight || 420;

  var grafico = LightweightCharts.createChart(elemento, {
    width: largura,
    height: altura,
    layout: {
      background: { color: "transparent" },
      textColor: corTexto
    },
    grid: {
      vertLines: { color: corGrade },
      horzLines: { color: corGrade }
    },
    rightPriceScale: { borderColor: corGrade },
    timeScale: { borderColor: corGrade, timeVisible: false },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal }
  });

  var cores = ["#0b64c4", "#b45309", "#7a2f9e", "#0f6b37", "#a11a1a"];

  if (dados.tipo === "comparacao") {
    (dados.series || []).forEach(function (serie, indice) {
      var linha = grafico.addLineSeries({
        color: cores[indice % cores.length],
        lineWidth: 2,
        title: serie.rotulo
      });
      linha.setData(serie.dados || []);
    });
  } else {
    var candles = grafico.addCandlestickSeries({
      upColor: "#0f6b37",
      downColor: "#a11a1a",
      borderUpColor: "#0f6b37",
      borderDownColor: "#a11a1a",
      wickUpColor: "#0f6b37",
      wickDownColor: "#a11a1a"
    });
    candles.setData(dados.candles || []);

    var volume = grafico.addHistogramSeries({
      priceFormat: { type: "volume" },
      priceScaleId: "volume"
    });
    volume.priceScale().applyOptions({
      scaleMargins: { top: 0.82, bottom: 0 }
    });
    var dadosVolume = (dados.volumes || []).map(function (ponto) {
      return {
        time: ponto.time,
        value: ponto.value,
        color: escuro ? "rgba(156, 199, 255, 0.45)" : "rgba(11, 79, 156, 0.35)"
      };
    });
    volume.setData(dadosVolume);

    (dados.medias || []).forEach(function (media, indice) {
      var linha = grafico.addLineSeries({
        color: cores[indice % cores.length],
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
        title: media.rotulo
      });
      linha.setData(media.dados || []);
    });

    var bandas = dados.bandas;
    if (bandas) {
      var corBanda = escuro ? "#8a94a6" : "#6b7684";
      var corMediaBanda = escuro ? "#c9a227" : "#8a6d1a";
      var rotulo = "Bollinger " + (bandas.janela || 20) + " (" + (bandas.desvios || 2) + "σ)";
      ["superior", "inferior"].forEach(function (nome) {
        var serie = grafico.addLineSeries({
          color: corBanda,
          lineWidth: 1,
          lineStyle: LightweightCharts.LineStyle.Dotted,
          priceLineVisible: false,
          lastValueVisible: false,
          // um rótulo só, na banda de cima, para não empilhar textos à direita
          title: nome === "superior" ? rotulo : ""
        });
        serie.setData(bandas[nome] || []);
      });
      var centro = grafico.addLineSeries({
        color: corMediaBanda,
        lineWidth: 1,
        lineStyle: LightweightCharts.LineStyle.Dashed,
        priceLineVisible: false,
        lastValueVisible: false,
        title: ""
      });
      centro.setData(bandas.media || []);
    }
  }

  grafico.timeScale().fitContent();

  var redimensionar = function () {
    grafico.applyOptions({ width: elemento.clientWidth || largura });
  };
  if (typeof ResizeObserver !== "undefined") {
    new ResizeObserver(redimensionar).observe(elemento);
  } else {
    window.addEventListener("resize", redimensionar);
  }
})();
