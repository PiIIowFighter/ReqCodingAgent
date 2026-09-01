#!/bin/sh
set -eu
test -f index.html && test -f styles.css && test -f app.js && test -f stocks.json
grep -q 'id="stock-list"' index.html
grep -q 'fetch("stocks.json")' app.js
grep -q 'function renderStocks' app.js
grep -Eq 'stock\.code|stock\["code"\]' app.js
grep -Eq 'stock\.name|stock\["name"\]' app.js
grep -Eq 'stock\.price|stock\["price"\]' app.js
grep -Eq 'stock\.change|stock\["change"\]' app.js
if grep -Eq 'const[[:space:]]+stocks[[:space:]]*=' app.js; then
  echo "duplicate local stocks declaration" >&2
  exit 1
fi
grep -Eq 'toLowerCase\(\).*includes|includes\(.*toLowerCase' app.js
grep -Eq 'no.{0,20}match|没有.{0,20}匹配|未找到' app.js
printf '%s\n' 'stock search checks passed'
