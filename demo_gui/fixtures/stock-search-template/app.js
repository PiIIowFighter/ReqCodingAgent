const stockList = document.querySelector("#stock-list");
const resultCount = document.querySelector("#result-count");

fetch("stocks.json")
  .then(response => response.json())
  .then(stocks => renderStocks(stocks));

function renderStocks(stocks) {
  resultCount.textContent = `${stocks.length} 只`;
  stockList.innerHTML = stocks.map(stock => `
    <article class="stock-row">
      <span class="stock-code">${stock.code}</span>
      <strong>${stock.name}</strong>
      <span class="stock-price">¥${stock.price.toFixed(2)}</span>
      <span class="${stock.change.startsWith("-") ? "down" : "up"}">${stock.change}</span>
  </article>`).join("");
}
