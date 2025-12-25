import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import random
import json
from concurrent.futures import ProcessPoolExecutor
import os
import time
import threading

# -----------------------------------------------------------
# CONFIG
# -----------------------------------------------------------

# This is my default configuration. In general, the larger the value, the more accurate the result.
# Underestimating TOP3_ITERATIONS will reduce the chances of rare and unpopular menu solutions being selected.
# Underestimating TOP3_SIMS_PER_EVAL will lead to greater price deviation and greater chaos in the results.
# At least till lategame TOP3_MAX_ING_PER_CAT = 10 is upper cap so feel free to set around 5-7

TOP3_ITERATIONS = 1000  # How many random menus to try
TOP3_SIMS_PER_EVAL = 20000  # How many simulations to use per menu evaluation
TOP3_MAX_ING_PER_CAT = 10  # Max ingredients per category in the menu
TOP3_REFINE_STEPS = 50  # Local improvement steps for each top candidate
CPU_AMOUNT = None  # Number of processes for parallel calculation (None = os.cpu_count())

ING_SCORE_FINISH_WEIGHT = 10  # Penalty weight for finish_chance in top-ingredient heuristic

DEFAULT_BOTTOM_BUN_PRICE = 5.0
DEFAULT_BOTTOM_BUN_BONUS = 0.0
DEFAULT_TOP_BUN_PRICE = 5.0
DEFAULT_TOP_BUN_BONUS = 0.0

LAST_MENU_STATE_FILE = "last_menu_path.json"


def simulate_burger(menu, simulations=None):
    if simulations is None:
        simulations = TOP3_SIMS_PER_EVAL
    total_money = 0

    for _ in range(simulations):
        used_categories = set()
        seen_ingredients = set()
        price = menu["bottom_bun"]["price"] + menu["bottom_bun"]["unique_bonus"]
        finished = False

        while not finished:
            categories = list(menu["categories"].keys())
            cat = random.choice(categories)
            valid_items = [ing for ing in menu["categories"][cat] if ing.get("enabled", True)]
            if not valid_items:
                continue
            ingredient = random.choice(valid_items)
            ing_name = ingredient["name"]

            if cat not in used_categories:
                used_categories.add(cat)
                is_new_global = ing_name not in seen_ingredients
                if is_new_global:
                    seen_ingredients.add(ing_name)
                    price += ingredient["price"] + ingredient["unique_bonus"]
                else:
                    price += ingredient["price"]
                continue

            if ing_name in seen_ingredients:
                if random.random() < 0.5:
                    finished = True
                else:
                    price += ingredient["price"]
                continue
            else:
                seen_ingredients.add(ing_name)
                price += ingredient["price"] + ingredient["unique_bonus"]
                if random.random() < ingredient["finish_chance"]:
                    finished = True

        price += menu["top_bun"]["price"] + menu["top_bun"]["unique_bonus"]
        total_money += price

    return total_money / simulations


def eval_categories_job(args):
    menu, categories_cfg, sims = args
    test_menu = {
        "bottom_bun": menu["bottom_bun"],
        "top_bun": menu["top_bun"],
        "categories": categories_cfg,
    }
    income = simulate_burger(test_menu, simulations=sims)
    return income


def compute_top3_menus(menu, iterations=None, sims_per_eval=None, max_ing_per_cat=None, refine_steps=None,
                       workers=None):
    if iterations is None:
        iterations = TOP3_ITERATIONS
    if sims_per_eval is None:
        sims_per_eval = TOP3_SIMS_PER_EVAL
    if max_ing_per_cat is None:
        max_ing_per_cat = TOP3_MAX_ING_PER_CAT
    if refine_steps is None:
        refine_steps = TOP3_REFINE_STEPS

    def random_menu_categories():
        cats_cfg = {}
        for cat, items in menu["categories"].items():
            enabled_items = [ing for ing in items if ing.get("enabled", True)]
            if not enabled_items:
                cats_cfg[cat] = []
                continue
            k = random.randint(1, min(len(enabled_items), max_ing_per_cat))
            cats_cfg[cat] = random.sample(enabled_items, k)
        return cats_cfg

    def eval_categories(categories_cfg, sims):
        test_menu = {
            "bottom_bun": menu["bottom_bun"],
            "top_bun": menu["top_bun"],
            "categories": categories_cfg,
        }
        income = simulate_burger(test_menu, simulations=sims)
        return income

    def refine_menu(start_cfg):
        current_cfg = {cat: list(ings) for cat, ings in start_cfg.items()}
        current_income = eval_categories(current_cfg, sims_per_eval)

        all_cats = list(menu["categories"].keys())

        for _ in range(refine_steps):
            cat = random.choice(all_cats)
            all_items = menu["categories"][cat]
            if not all_items:
                continue

            selected = current_cfg.get(cat, [])
            ops = []
            for ing in selected:
                ops.append(("remove", ing))
            if len(selected) < max_ing_per_cat:
                for ing in all_items:
                    if ing not in selected:
                        ops.append(("add", ing))

            if not ops:
                continue

            op, ing = random.choice(ops)
            new_cfg = {c: list(v) for c, v in current_cfg.items()}
            if op == "remove":
                new_cfg[cat] = [x for x in selected if x is not ing]
            else:
                new_cfg.setdefault(cat, []).append(ing)

            new_income = eval_categories(new_cfg, sims_per_eval)
            if new_income > current_income:
                current_cfg = new_cfg
                current_income = new_income

        return current_income, current_cfg

    configs = [random_menu_categories() for _ in range(iterations)]

    jobs = [(menu, cfg, sims_per_eval) for cfg in configs]
    if workers is None:
        workers = CPU_AMOUNT if CPU_AMOUNT is not None else os.cpu_count()

    if workers and workers > 1:
        with ProcessPoolExecutor(max_workers=workers) as executor:
            incomes = list(executor.map(eval_categories_job, jobs))
    else:
        incomes = [eval_categories(cfg, sims_per_eval) for cfg in configs]

    best = []
    for cfg, income in zip(configs, incomes):
        best.append((income, cfg))
        best.sort(key=lambda x: x[0], reverse=True)
        best = best[:3]

    refined = []
    for income, cfg in best:
        r_income, r_cfg = refine_menu(cfg)
        refined.append((r_income, r_cfg))

    refined.sort(key=lambda x: x[0], reverse=True)
    return refined[:3]


def compute_top3_ingredients(menu):
    ranking = []
    for cat, items in menu["categories"].items():
        for ing in items:
            if not ing.get("enabled", True):
                continue
            score = ing["price"] + ing["unique_bonus"] - ing["finish_chance"] * ING_SCORE_FINISH_WEIGHT
            ranking.append((score, cat, ing))
    ranking.sort(reverse=True, key=lambda x: x[0])
    return ranking[:3]


class BurgerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Burger Profit Optimizer Extended")
        self.categories = {}
        self.use_all_cores = tk.BooleanVar(value=True)
        self.last_menu_path = None
        self.loading_window = None
        self.build_gui()
        self.load_last_menu_if_exists()

    def build_gui(self):
        frame = ttk.Frame(self.root)
        frame.pack(fill="both", expand=True, padx=10, pady=10)

        self.root.rowconfigure(0, weight=1)
        self.root.columnconfigure(0, weight=1)
        for col in range(5):
            frame.columnconfigure(col, weight=1)
        for row in (1, 4):
            frame.rowconfigure(row, weight=1)

        ttk.Label(frame, text="Category:").grid(row=0, column=1)
        self.cat_name = tk.Entry(frame)
        self.cat_name.grid(row=0, column=2)
        ttk.Button(frame, text="Add Category", command=self.add_category).grid(row=0, column=3)

        self.cat_list = tk.Listbox(frame, width=35, height=6)
        self.cat_list.grid(row=1, column=0, columnspan=5, pady=10, sticky="nsew")
        self.cat_list.bind("<<ListboxSelect>>", self.update_ingredient_table)

        ttk.Label(frame, text="Name").grid(row=2, column=0)
        ttk.Label(frame, text="Price").grid(row=2, column=1)
        ttk.Label(frame, text="Unique Bonus").grid(row=2, column=2)
        ttk.Label(frame, text="Finish Chance").grid(row=2, column=3)

        self.ing_name = tk.Entry(frame)
        self.ing_price = tk.Entry(frame)
        self.ing_bonus = tk.Entry(frame)
        self.ing_finish = tk.Entry(frame)

        self.ing_name.grid(row=3, column=0)
        self.ing_price.grid(row=3, column=1)
        self.ing_bonus.grid(row=3, column=2)
        self.ing_finish.grid(row=3, column=3)

        ttk.Button(frame, text="Add Ingredient", command=self.add_ingredient).grid(row=3, column=4)

        self.table = ttk.Treeview(frame, columns=("name", "price", "bonus", "chance", "enabled"), show="headings",
                                  height=7)
        self.table.grid(row=4, column=0, columnspan=5, pady=10, sticky="nsew")
        self.table.heading("name", text="Name", command=lambda: self.sort_table("name", False))
        self.table.heading("price", text="Price", command=lambda: self.sort_table("price", False))
        self.table.heading("bonus", text="Unique Bonus", command=lambda: self.sort_table("bonus", False))
        self.table.heading("chance", text="Finish Chance", command=lambda: self.sort_table("chance", False))
        self.table.heading("enabled", text="In Top 3", command=lambda: self.sort_table("enabled", False))
        self.table.bind("<Double-1>", self.edit_cell)

        ttk.Label(frame, text="Bottom Bun Price:").grid(row=5, column=1)
        self.bottom_price = tk.Entry(frame)
        self.bottom_price.insert(0, str(DEFAULT_BOTTOM_BUN_PRICE))
        self.bottom_price.grid(row=5, column=2)

        ttk.Label(frame, text="Uniqueness Bonus:").grid(row=5, column=3)
        self.bottom_bonus = tk.Entry(frame)
        self.bottom_bonus.insert(0, str(DEFAULT_BOTTOM_BUN_BONUS))
        self.bottom_bonus.grid(row=5, column=4)

        ttk.Label(frame, text="Top Bun Price:").grid(row=6, column=1)
        self.top_price = tk.Entry(frame)
        self.top_price.insert(0, str(DEFAULT_TOP_BUN_PRICE))
        self.top_price.grid(row=6, column=2)

        ttk.Label(frame, text="Uniqueness Bonus:").grid(row=6, column=3)
        self.top_bonus = tk.Entry(frame)
        self.top_bonus.insert(0, str(DEFAULT_TOP_BUN_BONUS))
        self.top_bonus.grid(row=6, column=4)

        ttk.Button(frame, text="Save Menu", command=self.save_menu).grid(row=7, column=1)
        ttk.Button(frame, text="Load Menu", command=self.load_menu).grid(row=7, column=2)
        self.top3_button = ttk.Button(frame, text="Top 3", command=self.show_top3)
        self.top3_button.grid(row=7, column=3)
        tk.Checkbutton(frame, text="Use all cores", variable=self.use_all_cores).grid(row=7, column=4, sticky="w")

    def add_category(self):
        name = self.cat_name.get().strip()
        if name and name not in self.categories:
            self.categories[name] = []
            self.cat_list.insert(tk.END, name)
        self.cat_name.delete(0, tk.END)

    def add_ingredient(self):
        try:
            cat = self.cat_list.get(self.cat_list.curselection())
        except:
            messagebox.showerror("Error", "Select a category")
            return

        try:
            ing = {
                "name": self.ing_name.get(),
                "price": float(self.ing_price.get()),
                "finish_chance": float(self.ing_finish.get()),
                "unique_bonus": float(self.ing_bonus.get()),
                "enabled": True,
            }
        except:
            messagebox.showerror("Error", "Check ingredient data")
            return

        self.categories[cat].append(ing)
        self.categories[cat].sort(key=lambda i: i["name"])
        self.ing_name.delete(0, tk.END)
        self.ing_price.delete(0, tk.END)
        self.ing_finish.delete(0, tk.END)
        self.ing_bonus.delete(0, tk.END)
        self.update_ingredient_table()

    def update_ingredient_table(self, event=None):
        for row in self.table.get_children():
            self.table.delete(row)
        try:
            cat = self.cat_list.get(self.cat_list.curselection())
        except:
            return
        for ing in sorted(self.categories[cat], key=lambda i: i["name"]):
            enabled = ing.get("enabled", True)
            self.table.insert("", "end", values=(
            ing["name"], ing["price"], ing["unique_bonus"], ing["finish_chance"], "Yes" if enabled else "No"))

    def sort_table(self, col, reverse):
        items = [(self.table.set(k, col), k) for k in self.table.get_children('')]

        def conv(v):
            try:
                return float(v)
            except:
                return v

        items.sort(key=lambda t: conv(t[0]), reverse=reverse)

        for index, (_, k) in enumerate(items):
            self.table.move(k, '', index)

        self.table.heading(col, command=lambda: self.sort_table(col, not reverse))

    def edit_cell(self, event):
        item = self.table.selection()[0]
        values = self.table.item(item, "values")
        if not values:
            return
        name = values[0]
        cat = self.cat_list.get(self.cat_list.curselection())
        for ing in self.categories[cat]:
            if ing["name"] == name:
                edit_win = tk.Toplevel(self.root)
                edit_win.title(name)

                tk.Label(edit_win, text="Price").grid(row=0, column=0)
                e1 = tk.Entry(edit_win);
                e1.insert(0, str(ing["price"]));
                e1.grid(row=0, column=1)
                tk.Label(edit_win, text="Chance").grid(row=1, column=0)
                e2 = tk.Entry(edit_win);
                e2.insert(0, str(ing["finish_chance"]));
                e2.grid(row=1, column=1)
                tk.Label(edit_win, text="Bonus").grid(row=2, column=0)
                e3 = tk.Entry(edit_win);
                e3.insert(0, str(ing["unique_bonus"]));
                e3.grid(row=2, column=1)
                var_enabled = tk.BooleanVar(value=ing.get("enabled", True))
                tk.Checkbutton(edit_win, text="Use in Top 3", variable=var_enabled).grid(row=3, column=0, columnspan=2)

                def save():
                    ing["price"] = float(e1.get())
                    ing["finish_chance"] = float(e2.get())
                    ing["unique_bonus"] = float(e3.get())
                    ing["enabled"] = bool(var_enabled.get())
                    edit_win.destroy()
                    self.update_ingredient_table()

                tk.Button(edit_win, text="OK", command=save).grid(row=4, column=0, columnspan=2)
                break

    def save_menu(self):
        data = {
            "bottom_bun": {"price": float(self.bottom_price.get()), "unique_bonus": float(self.bottom_bonus.get())},
            "top_bun": {"price": float(self.top_price.get()), "unique_bonus": float(self.top_bonus.get())},
            "categories": self.categories
        }
        path = filedialog.asksaveasfilename(defaultextension=".json")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
            self.last_menu_path = path
            self.save_last_menu_path()

    def load_menu(self):
        path = filedialog.askopenfilename(filetypes=[("JSON", "*.json")])
        if not path:
            return
        self.load_menu_from_path(path)
        self.last_menu_path = path
        self.save_last_menu_path()

    def load_menu_from_path(self, path):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.bottom_price.delete(0, tk.END)
        self.bottom_price.insert(0, data.get("bottom_bun", {}).get("price", 0))
        self.bottom_bonus.delete(0, tk.END)
        self.bottom_bonus.insert(0, data.get("bottom_bun", {}).get("unique_bonus", 0))

        self.top_price.delete(0, tk.END)
        self.top_price.insert(0, data.get("top_bun", {}).get("price", 0))
        self.top_bonus.delete(0, tk.END)
        self.top_bonus.insert(0, data.get("top_bun", {}).get("unique_bonus", 0))

        loaded_cats = data.get("categories", {})
        if isinstance(loaded_cats, list):
            cats = {}
            for c in loaded_cats:
                cats[c["name"]] = c.get("items", [])
            self.categories = cats
        else:
            self.categories = loaded_cats

        for cat, items in self.categories.items():
            for ing in items:
                if "enabled" not in ing:
                    ing["enabled"] = True
            items.sort(key=lambda i: i["name"])

        self.cat_list.delete(0, tk.END)
        for cat in self.categories:
            self.cat_list.insert(tk.END, cat)

        if self.cat_list.size() > 0:
            self.cat_list.selection_clear(0, tk.END)
            self.cat_list.selection_set(0)
            try:
                self.cat_list.activate(0)
            except Exception:
                pass
            self.update_ingredient_table()

    def save_last_menu_path(self):
        if not self.last_menu_path:
            return
        try:
            with open(LAST_MENU_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump({"last_path": self.last_menu_path}, f)
        except Exception:
            pass

    def load_last_menu_if_exists(self):
        try:
            if os.path.exists(LAST_MENU_STATE_FILE):
                with open(LAST_MENU_STATE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                path = data.get("last_path")
                if path and os.path.exists(path):
                    self.last_menu_path = path
                    self.load_menu_from_path(path)
        except Exception:
            pass

    def show_top3(self):
        if self.loading_window is not None:
            return

        self.loading_window = tk.Toplevel(self.root)
        self.loading_window.title("Calculating Top 3")
        self.loading_window.transient(self.root)
        self.loading_window.grab_set()
        ttk.Label(self.loading_window, text="Calculating Top 3, please wait...").pack(padx=20, pady=10)
        pb = ttk.Progressbar(self.loading_window, mode="indeterminate")
        pb.pack(fill="x", padx=20, pady=(0, 10))
        pb.start(10)

        self.loading_window.update_idletasks()
        root_x = self.root.winfo_x()
        root_y = self.root.winfo_y()
        root_w = self.root.winfo_width()
        root_h = self.root.winfo_height()
        win_w = self.loading_window.winfo_width()
        win_h = self.loading_window.winfo_height()
        x = root_x + (root_w - win_w) // 2
        y = root_y + (root_h - win_h) // 2
        self.loading_window.geometry(f"{win_w}x{win_h}+{x}+{y}")

        self.top3_button.config(state="disabled")

        thread = threading.Thread(target=self._compute_top3_async, daemon=True)
        thread.start()

    def _compute_top3_async(self):
        menu = {
            "bottom_bun": {"price": float(self.bottom_price.get()), "unique_bonus": float(self.bottom_bonus.get())},
            "top_bun": {"price": float(self.top_price.get()), "unique_bonus": float(self.top_bonus.get())},
            "categories": self.categories,
        }

        workers = None if self.use_all_cores.get() else 1

        start_time = time.time()
        top3_menus = compute_top3_menus(menu, workers=workers)
        top3_ingredients = compute_top3_ingredients(menu)
        elapsed = time.time() - start_time

        self.root.after(0, self._show_top3_result, top3_menus, top3_ingredients, elapsed)

    def _show_top3_result(self, top3_menus, top3_ingredients, elapsed):
        if self.loading_window is not None:
            try:
                self.loading_window.destroy()
            except Exception:
                pass
            self.loading_window = None

        self.top3_button.config(state="normal")

        lines = []
        lines.append(f"Calculation Time: {elapsed:.2f} s")
        lines.append("")
        lines.append("Top 3 Menus:")
        for i, (income, categories_dict) in enumerate(top3_menus, start=1):
            block_lines = [f"Menu #{i}: Income {round(income, 2)}"]
            for cat, ings in categories_dict.items():
                if not ings:
                    block_lines.append(f"  {cat}: <empty>")
                else:
                    ing_list = []
                    for ing in ings:
                        ing_list.append(
                            f"{ing['name']} (Price {ing['price']}, Chance {ing['finish_chance']}, Bonus {ing['unique_bonus']})")
                    block_lines.append(f"  {cat}: {', '.join(ing_list)}")
            lines.append("\n".join(block_lines))

        lines.append("")

        messagebox.showinfo("Top 3", "\n\n".join(lines))


if __name__ == "__main__":
    root = tk.Tk()
    burger = BurgerApp(root)
    root.mainloop()
