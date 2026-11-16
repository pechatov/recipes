import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const script = readFileSync(
  new URL("../recipes/static/recipes/shopping.js", import.meta.url), "utf8"
);

function shoppingList(items) {
  const count = { textContent: "" };
  const pantryToggle = new EventTarget();
  const checkboxes = items.map(item => Object.assign(new EventTarget(), item));
  const rows = checkboxes.map(checkbox => ({
    classList: {
      toggle(_name, checkedOff) { checkbox.checkedOff = checkedOff; },
    },
    querySelector() { return checkbox; },
  }));
  const selectors = {
    "[data-shopping-item]": rows,
    "[data-pantry-checkbox]": checkboxes.filter(checkbox => checkbox.pantry),
    "[data-selected-item-count]": count,
    "[data-pantry-toggle]": pantryToggle,
    "[data-copy-list]": new EventTarget(),
  };
  const container = {
    querySelector(selector) { return selectors[selector]; },
    querySelectorAll(selector) { return selectors[selector]; },
  };
  vm.runInNewContext(script, {
    document: {
      querySelector(selector) {
        return selector === "[data-shopping-list]" ? container : null;
      },
    },
    Event,
  });
  const select = (checkbox, checked) => {
    checkbox.checked = checked;
    checkbox.dispatchEvent(new Event("change"));
  };
  return { count, checkboxes, pantryToggle, select };
}

test("counts selected products on load and after individual changes", () => {
  const { count, checkboxes, select } = shoppingList([
    { checked: true },
    { checked: true },
    { checked: false, pantry: true },
    { checked: false, pantry: true },
  ]);
  assert.equal(Number(count.textContent), 2);
  select(checkboxes[0], false);
  assert.equal(Number(count.textContent), 1);
  assert.equal(checkboxes[0].checkedOff, true);
  select(checkboxes[2], true);
  assert.equal(Number(count.textContent), 2);
  select(checkboxes[1], false);
  select(checkboxes[2], false);
  assert.equal(Number(count.textContent), 0);
});

test("bulk pantry changes update the count and preserve other selections", () => {
  const { count, checkboxes, pantryToggle, select } = shoppingList([
    { checked: true },
    { checked: false },
    { checked: false, pantry: true },
    { checked: false, pantry: true },
  ]);
  select(checkboxes[2], true);
  assert.equal(pantryToggle.indeterminate, true);
  assert.equal(Number(count.textContent), 2);
  select(pantryToggle, true);
  assert.equal(Number(count.textContent), 3);
  assert.equal(pantryToggle.indeterminate, false);
  select(pantryToggle, false);
  assert.equal(Number(count.textContent), 1);
  assert.equal(checkboxes[0].checked, true);
  assert.equal(checkboxes[1].checked, false);
});

test("shows zero for an empty list", () => {
  const { count } = shoppingList([]);
  assert.equal(Number(count.textContent), 0);
});
