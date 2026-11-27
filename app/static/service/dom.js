export const $ = id => document.getElementById(id);

export function option(value, text) {
  const node = document.createElement("option");
  node.value = value;
  node.textContent = text;
  return node;
}

export function item(text) {
  const node = document.createElement("li");
  node.textContent = text;
  return node;
}

export function choices(select, rows, prompt, format) {
  const previous = select.value;
  select.replaceChildren(option("", prompt), ...rows.map(row => option(row.id, format(row))));
  if (rows.some(row => row.id === previous)) select.value = previous;
}

export function when(seconds) {
  return new Date(seconds * 1000).toLocaleString("zh-CN", { hour12: false });
}
