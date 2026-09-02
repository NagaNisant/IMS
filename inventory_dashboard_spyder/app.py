from flask import Flask, render_template, request, redirect, url_for, flash
import sqlite3
from datetime import datetime, date
import os

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "inventory-dashboard-secret")

DB_NAME = "inventory_boxes.db"
MAX_PALLETS = 10


# =========================================================
# DATABASE
# =========================================================

def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                box_weight REAL NOT NULL CHECK (box_weight > 0)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS pallets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pallet_no INTEGER NOT NULL UNIQUE
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS stock (
                product_id INTEGER NOT NULL,
                pallet_id INTEGER NOT NULL,
                boxes REAL NOT NULL DEFAULT 0 CHECK (boxes >= 0),
                PRIMARY KEY (product_id, pallet_id),
                FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE,
                FOREIGN KEY (pallet_id) REFERENCES pallets(id) ON DELETE CASCADE
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                pallet_id INTEGER NOT NULL,
                movement_type TEXT NOT NULL
                    CHECK (movement_type IN ('Inward', 'Outward')),
                boxes REAL NOT NULL CHECK (boxes > 0),
                created_at TEXT NOT NULL,
                transaction_date TEXT NOT NULL,
                FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE,
                FOREIGN KEY (pallet_id) REFERENCES pallets(id) ON DELETE CASCADE
            )
        """)

        # Always keep P01-P10 available.
        for pallet_no in range(1, MAX_PALLETS + 1):
            conn.execute(
                "INSERT OR IGNORE INTO pallets (pallet_no) VALUES (?)",
                (pallet_no,)
            )

        conn.commit()
    finally:
        conn.close()


# =========================================================
# HELPERS
# =========================================================

def format_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%d-%b-%Y")
    except (TypeError, ValueError):
        return value


def valid_date(value):
    try:
        datetime.strptime(str(value).strip(), "%Y-%m-%d")
        return True
    except (TypeError, ValueError):
        return False


def positive_float(value):
    try:
        number = float(str(value).strip())
        return number if number > 0 else None
    except (TypeError, ValueError):
        return None


def positive_int(value):
    try:
        number = int(str(value).strip())
        return number if number > 0 else None
    except (TypeError, ValueError):
        return None


def get_transaction_dates(conn):
    rows = conn.execute("""
        SELECT DISTINCT transaction_date
        FROM transactions
        ORDER BY transaction_date DESC
    """).fetchall()
    return [row["transaction_date"] for row in rows]


def stock_balance_as_of(conn, product_id, pallet_id, transaction_date):
    """Return product/pallet balance as of a given date."""
    row = conn.execute("""
        SELECT COALESCE(SUM(
            CASE
                WHEN movement_type = 'Inward' THEN boxes
                WHEN movement_type = 'Outward' THEN -boxes
                ELSE 0
            END
        ), 0) AS balance
        FROM transactions
        WHERE product_id = ?
          AND pallet_id = ?
          AND transaction_date <= ?
    """, (product_id, pallet_id, transaction_date)).fetchone()

    return float(row["balance"] or 0)


def rebuild_current_stock(conn):
    """
    Rebuild the current stock table from the complete transaction history.

    This is important when transactions can be entered with a historical
    transaction date. Current stock must not depend on the order in which
    the user happened to enter the transactions.
    """
    conn.execute("DELETE FROM stock")

    rows = conn.execute("""
        SELECT
            product_id,
            pallet_id,
            SUM(
                CASE
                    WHEN movement_type = 'Inward' THEN boxes
                    WHEN movement_type = 'Outward' THEN -boxes
                    ELSE 0
                END
            ) AS boxes
        FROM transactions
        GROUP BY product_id, pallet_id
        HAVING boxes > 0
    """).fetchall()

    for row in rows:
        conn.execute("""
            INSERT INTO stock (product_id, pallet_id, boxes)
            VALUES (?, ?, ?)
        """, (row["product_id"], row["pallet_id"], row["boxes"]))


def current_stock(conn):
    """Return current stock grouped by product."""
    return conn.execute("""
        SELECT
            p.id,
            p.name,
            p.box_weight,
            COALESCE(SUM(s.boxes), 0) AS total_boxes,
            COALESCE(SUM(s.boxes * p.box_weight), 0) AS total_weight,
            COUNT(CASE WHEN s.boxes > 0 THEN 1 END) AS pallet_count
        FROM products p
        LEFT JOIN stock s ON p.id = s.product_id
        GROUP BY p.id
        HAVING total_boxes > 0
        ORDER BY p.name COLLATE NOCASE
    """).fetchall()


# =========================================================
# DASHBOARD
# =========================================================

@app.route("/")
def dashboard():
    selected_date = request.args.get("date", "total")

    conn = get_db()
    try:
        all_products = conn.execute("""
            SELECT id, name, box_weight
            FROM products
            ORDER BY name COLLATE NOCASE
        """).fetchall()

        dates = get_transaction_dates(conn)

        if selected_date == "total":
            products = current_stock(conn)

            totals = conn.execute("""
                SELECT
                    COALESCE(SUM(s.boxes), 0) AS boxes,
                    COALESCE(SUM(s.boxes * p.box_weight), 0) AS weight
                FROM stock s
                JOIN products p ON p.id = s.product_id
                WHERE s.boxes > 0
            """).fetchone()

            total_boxes = float(totals["boxes"] or 0)
            total_stock = float(totals["weight"] or 0)

            total_inward = conn.execute("""
                SELECT COALESCE(SUM(t.boxes * p.box_weight), 0) AS total
                FROM transactions t
                JOIN products p ON p.id = t.product_id
                WHERE t.movement_type = 'Inward'
            """).fetchone()["total"]

            total_outward = conn.execute("""
                SELECT COALESCE(SUM(t.boxes * p.box_weight), 0) AS total
                FROM transactions t
                JOIN products p ON p.id = t.product_id
                WHERE t.movement_type = 'Outward'
            """).fetchone()["total"]

            display_date = None

        else:
            if not valid_date(selected_date):
                flash("Invalid date selected.", "error")
                return redirect(url_for("dashboard"))

            # IMPORTANT:
            # Historical stock is reconstructed separately for every
            # product + pallet. Therefore today's pallet allocation cannot
            # leak into an older date.
            rows = conn.execute("""
                SELECT
                    t.product_id,
                    t.pallet_id,
                    SUM(
                        CASE
                            WHEN t.movement_type = 'Inward' THEN t.boxes
                            WHEN t.movement_type = 'Outward' THEN -t.boxes
                            ELSE 0
                        END
                    ) AS boxes
                FROM transactions t
                WHERE t.transaction_date <= ?
                GROUP BY t.product_id, t.pallet_id
                HAVING boxes > 0
            """, (selected_date,)).fetchall()

            totals_by_product = {}

            for row in rows:
                product_id = row["product_id"]
                boxes = float(row["boxes"])

                if product_id not in totals_by_product:
                    totals_by_product[product_id] = {
                        "boxes": 0.0,
                        "pallet_count": 0
                    }

                totals_by_product[product_id]["boxes"] += boxes
                totals_by_product[product_id]["pallet_count"] += 1

            products = []

            for p in all_products:
                data = totals_by_product.get(p["id"])
                if not data:
                    continue

                products.append({
                    "id": p["id"],
                    "name": p["name"],
                    "box_weight": p["box_weight"],
                    "total_boxes": data["boxes"],
                    "total_weight": data["boxes"] * p["box_weight"],
                    "pallet_count": data["pallet_count"]
                })

            total_boxes = sum(p["total_boxes"] for p in products)
            total_stock = sum(p["total_weight"] for p in products)

            # Movement figures for the selected day.
            total_inward = conn.execute("""
                SELECT COALESCE(SUM(t.boxes * p.box_weight), 0) AS total
                FROM transactions t
                JOIN products p ON p.id = t.product_id
                WHERE t.movement_type = 'Inward'
                  AND t.transaction_date = ?
            """, (selected_date,)).fetchone()["total"]

            total_outward = conn.execute("""
                SELECT COALESCE(SUM(t.boxes * p.box_weight), 0) AS total
                FROM transactions t
                JOIN products p ON p.id = t.product_id
                WHERE t.movement_type = 'Outward'
                  AND t.transaction_date = ?
            """, (selected_date,)).fetchone()["total"]

            display_date = format_date(selected_date)

        return render_template(
            "dashboard.html",
            products=products,
            all_products=all_products,
            total_stock=total_stock,
            total_boxes=total_boxes,
            total_inward=total_inward,
            total_outward=total_outward,
            dates=dates,
            selected_date=selected_date,
            display_date=display_date,
            today=date.today().strftime("%Y-%m-%d")
        )
    finally:
        conn.close()


# =========================================================
# CREATE PRODUCT / ADD STOCK
# =========================================================

@app.route("/add_product", methods=["POST"])
def add_product():
    mode = request.form.get("product_mode", "").strip()

    # -----------------------------------------------------
    # CREATE PRODUCT
    # -----------------------------------------------------
    if mode == "create":
        name = request.form.get("name", "").strip()
        box_weight = positive_float(request.form.get("box_weight", ""))

        if not name:
            flash("Enter a product name.", "error")
            return redirect(url_for("dashboard"))

        if box_weight is None:
            flash("Enter a valid weight per box greater than zero.", "error")
            return redirect(url_for("dashboard"))

        conn = get_db()
        try:
            exists = conn.execute("""
                SELECT id
                FROM products
                WHERE LOWER(name) = LOWER(?)
            """, (name,)).fetchone()

            if exists:
                flash("Product already exists.", "error")
                return redirect(url_for("dashboard"))

            conn.execute("""
                INSERT INTO products (name, box_weight)
                VALUES (?, ?)
            """, (name, box_weight))

            conn.commit()
            flash(f"Product '{name}' created successfully.", "success")
            return redirect(url_for("dashboard"))

        except sqlite3.Error:
            conn.rollback()
            flash("Could not create the product.", "error")
            return redirect(url_for("dashboard"))
        finally:
            conn.close()

    # -----------------------------------------------------
    # ADD / REMOVE STOCK
    # -----------------------------------------------------
    product_id = positive_int(request.form.get("product_id", ""))
    boxes = positive_float(request.form.get("boxes", ""))
    pallet_no = positive_int(request.form.get("pallet_no", ""))
    transaction_date = request.form.get("transaction_date", "").strip()
    movement_type = request.form.get("movement_type", "Inward").strip()

    if product_id is None:
        flash("Please select a product.", "error")
        return redirect(url_for("dashboard"))

    if boxes is None:
        flash("Quantity must be greater than zero.", "error")
        return redirect(url_for("dashboard"))

    if pallet_no is None or pallet_no > MAX_PALLETS:
        flash(f"Pallet must be between P01 and P{MAX_PALLETS:02d}.", "error")
        return redirect(url_for("dashboard"))

    if not valid_date(transaction_date):
        flash("Please enter a valid transaction date.", "error")
        return redirect(url_for("dashboard"))

    if movement_type not in ("Inward", "Outward"):
        flash("Invalid movement type.", "error")
        return redirect(url_for("dashboard"))

    conn = get_db()
    try:
        product = conn.execute("""
            SELECT id, name
            FROM products
            WHERE id = ?
        """, (product_id,)).fetchone()

        if not product:
            flash("Product not found.", "error")
            return redirect(url_for("dashboard"))

        pallet = conn.execute("""
            SELECT id, pallet_no
            FROM pallets
            WHERE pallet_no = ?
        """, (pallet_no,)).fetchone()

        if not pallet:
            flash("Pallet not found.", "error")
            return redirect(url_for("dashboard"))

        # For historical outward transactions, validate against the stock
        # that actually existed on that pallet on that date.
        if movement_type == "Outward":
            balance = stock_balance_as_of(
                conn,
                product_id,
                pallet["id"],
                transaction_date
            )

            if boxes > balance:
                flash(
                    f"Cannot remove {boxes:g} boxes. "
                    f"Only {balance:g} boxes existed on P{pallet_no:02d} "
                    f"as of {format_date(transaction_date)}.",
                    "error"
                )
                return redirect(url_for("dashboard"))

        conn.execute("""
            INSERT INTO transactions (
                product_id,
                pallet_id,
                movement_type,
                boxes,
                created_at,
                transaction_date
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            product_id,
            pallet["id"],
            movement_type,
            boxes,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            transaction_date
        ))

        # Rebuild current stock from the complete transaction history.
        # This keeps current stock correct even when a transaction is
        # entered later with an older transaction date.
        rebuild_current_stock(conn)

        conn.commit()

        flash(
            f"{movement_type} stock updated successfully.",
            "success"
        )
        return redirect(url_for("dashboard"))

    except sqlite3.Error:
        conn.rollback()
        flash("A database error occurred while updating stock.", "error")
        return redirect(url_for("dashboard"))
    finally:
        conn.close()


# =========================================================
# PRODUCT PAGE
# =========================================================

@app.route("/product/<int:product_id>")
def product(product_id):
    selected_date = request.args.get("date", "total")

    conn = get_db()
    try:
        product_row = conn.execute("""
            SELECT *
            FROM products
            WHERE id = ?
        """, (product_id,)).fetchone()

        if not product_row:
            return "Product not found", 404

        if selected_date == "total":
            pallets = conn.execute("""
                SELECT
                    pa.pallet_no,
                    COALESCE(s.boxes, 0) AS boxes
                FROM pallets pa
                LEFT JOIN stock s
                    ON s.pallet_id = pa.id
                   AND s.product_id = ?
                WHERE COALESCE(s.boxes, 0) > 0
                ORDER BY pa.pallet_no
            """, (product_id,)).fetchall()

            history = conn.execute("""
                SELECT
                    t.movement_type,
                    t.boxes,
                    t.created_at,
                    t.transaction_date,
                    pa.pallet_no
                FROM transactions t
                JOIN pallets pa ON pa.id = t.pallet_id
                WHERE t.product_id = ?
                ORDER BY t.transaction_date DESC, t.id DESC
            """, (product_id,)).fetchall()

            display_date = None

        else:
            if not valid_date(selected_date):
                flash("Invalid date selected.", "error")
                return redirect(url_for("product", product_id=product_id))

            pallets = conn.execute("""
                SELECT
                    pa.pallet_no,
                    SUM(
                        CASE
                            WHEN t.movement_type = 'Inward' THEN t.boxes
                            WHEN t.movement_type = 'Outward' THEN -t.boxes
                            ELSE 0
                        END
                    ) AS boxes
                FROM pallets pa
                JOIN transactions t
                    ON t.pallet_id = pa.id
                   AND t.product_id = ?
                   AND t.transaction_date <= ?
                GROUP BY pa.id, pa.pallet_no
                HAVING boxes > 0
                ORDER BY pa.pallet_no
            """, (product_id, selected_date)).fetchall()

            history = conn.execute("""
                SELECT
                    t.movement_type,
                    t.boxes,
                    t.created_at,
                    t.transaction_date,
                    pa.pallet_no
                FROM transactions t
                JOIN pallets pa ON pa.id = t.pallet_id
                WHERE t.product_id = ?
                  AND t.transaction_date <= ?
                ORDER BY t.transaction_date DESC, t.id DESC
            """, (product_id, selected_date)).fetchall()

            display_date = format_date(selected_date)

        total_boxes = sum(float(p["boxes"]) for p in pallets)
        total_weight = total_boxes * product_row["box_weight"]

        return render_template(
            "product.html",
            product=product_row,
            pallets=pallets,
            total_boxes=total_boxes,
            total_weight=total_weight,
            history=history,
            selected_date=selected_date,
            display_date=display_date
        )
    finally:
        conn.close()


# =========================================================
# PALLET PAGE
# =========================================================

@app.route("/pallet/<int:pallet_no>")
def pallet(pallet_no):
    selected_date = request.args.get("date", "total")

    if pallet_no < 1 or pallet_no > MAX_PALLETS:
        return "Pallet not found", 404

    conn = get_db()
    try:
        pallet_row = conn.execute("""
            SELECT *
            FROM pallets
            WHERE pallet_no = ?
        """, (pallet_no,)).fetchone()

        if not pallet_row:
            return "Pallet not found", 404

        if selected_date == "total":
            products = conn.execute("""
                SELECT
                    p.id,
                    p.name,
                    p.box_weight,
                    s.boxes,
                    s.boxes * p.box_weight AS weight
                FROM stock s
                JOIN products p ON p.id = s.product_id
                WHERE s.pallet_id = ?
                  AND s.boxes > 0
                ORDER BY p.name COLLATE NOCASE
            """, (pallet_row["id"],)).fetchall()

            history = conn.execute("""
                SELECT
                    t.product_id,
                    p.name,
                    t.movement_type,
                    t.boxes,
                    t.created_at,
                    t.transaction_date
                FROM transactions t
                JOIN products p ON p.id = t.product_id
                WHERE t.pallet_id = ?
                ORDER BY t.transaction_date DESC, t.id DESC
            """, (pallet_row["id"],)).fetchall()

            display_date = None

        else:
            if not valid_date(selected_date):
                flash("Invalid date selected.", "error")
                return redirect(url_for("pallet", pallet_no=pallet_no))

            # Reconstruct this pallet's contents as of the selected date.
            products = conn.execute("""
                SELECT
                    p.id,
                    p.name,
                    p.box_weight,
                    SUM(
                        CASE
                            WHEN t.movement_type = 'Inward' THEN t.boxes
                            WHEN t.movement_type = 'Outward' THEN -t.boxes
                            ELSE 0
                        END
                    ) AS boxes,
                    SUM(
                        CASE
                            WHEN t.movement_type = 'Inward'
                                THEN t.boxes * p.box_weight
                            WHEN t.movement_type = 'Outward'
                                THEN -t.boxes * p.box_weight
                            ELSE 0
                        END
                    ) AS weight
                FROM transactions t
                JOIN products p ON p.id = t.product_id
                WHERE t.pallet_id = ?
                  AND t.transaction_date <= ?
                GROUP BY p.id, p.name, p.box_weight
                HAVING boxes > 0
                ORDER BY p.name COLLATE NOCASE
            """, (pallet_row["id"], selected_date)).fetchall()

            history = conn.execute("""
                SELECT
                    t.product_id,
                    p.name,
                    t.movement_type,
                    t.boxes,
                    t.created_at,
                    t.transaction_date
                FROM transactions t
                JOIN products p ON p.id = t.product_id
                WHERE t.pallet_id = ?
                  AND t.transaction_date <= ?
                ORDER BY t.transaction_date DESC, t.id DESC
            """, (pallet_row["id"], selected_date)).fetchall()

            display_date = format_date(selected_date)

        total_boxes = sum(float(p["boxes"]) for p in products)
        total_weight = sum(float(p["weight"]) for p in products)

        return render_template(
            "pallet.html",
            pallet=pallet_row,
            products=products,
            total_boxes=total_boxes,
            total_weight=total_weight,
            history=history,
            selected_date=selected_date,
            display_date=display_date
        )
    finally:
        conn.close()


# =========================================================
# CLEAR PALLET
# =========================================================

@app.route("/clear_pallet", methods=["POST"])
def clear_pallet():
    pallet_no = positive_int(request.form.get("pallet_no", ""))

    if pallet_no is None or pallet_no > MAX_PALLETS:
        flash("Invalid pallet number.", "error")
        return redirect(url_for("dashboard"))

    conn = get_db()
    try:
        pallet_row = conn.execute("""
            SELECT *
            FROM pallets
            WHERE pallet_no = ?
        """, (pallet_no,)).fetchone()

        if not pallet_row:
            flash("Pallet not found.", "error")
            return redirect(url_for("dashboard"))

        rows = conn.execute("""
            SELECT product_id, boxes
            FROM stock
            WHERE pallet_id = ?
              AND boxes > 0
        """, (pallet_row["id"],)).fetchall()

        if not rows:
            flash(f"P{pallet_no:02d} is already empty.", "error")
            return redirect(url_for("dashboard"))

        today = date.today().strftime("%Y-%m-%d")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for row in rows:
            conn.execute("""
                INSERT INTO transactions (
                    product_id,
                    pallet_id,
                    movement_type,
                    boxes,
                    created_at,
                    transaction_date
                )
                VALUES (?, ?, 'Outward', ?, ?, ?)
            """, (
                row["product_id"],
                pallet_row["id"],
                row["boxes"],
                now,
                today
            ))

        rebuild_current_stock(conn)
        conn.commit()

        flash(
            f"P{pallet_no:02d} has been cleared and the removal was recorded.",
            "success"
        )
        return redirect(url_for("dashboard"))

    except sqlite3.Error:
        conn.rollback()
        flash("A database error occurred while clearing the pallet.", "error")
        return redirect(url_for("dashboard"))
    finally:
        conn.close()


# =========================================================
# DELETE PRODUCT
# =========================================================

@app.route("/delete_product/<int:product_id>", methods=["POST"])
def delete_product(product_id):
    conn = get_db()
    try:
        product_row = conn.execute("""
            SELECT id, name
            FROM products
            WHERE id = ?
        """, (product_id,)).fetchone()

        if not product_row:
            flash("Product not found.", "error")
            return redirect(url_for("dashboard"))

        stock = conn.execute("""
            SELECT COALESCE(SUM(boxes), 0) AS total
            FROM stock
            WHERE product_id = ?
        """, (product_id,)).fetchone()

        if stock["total"] > 0:
            flash(
                "Cannot delete a product that still has stock. "
                "Remove the stock first.",
                "error"
            )
            return redirect(url_for("dashboard"))

        conn.execute(
            "DELETE FROM products WHERE id = ?",
            (product_id,)
        )
        conn.commit()

        flash(
            f"Product '{product_row['name']}' deleted successfully.",
            "success"
        )
        return redirect(url_for("dashboard"))

    except sqlite3.Error:
        conn.rollback()
        flash("A database error occurred while deleting the product.", "error")
        return redirect(url_for("dashboard"))
    finally:
        conn.close()


# =========================================================
# CLEAR ALL INVENTORY DATA
# =========================================================

@app.route("/clear_all_data", methods=["POST"])
def clear_all_data():
    conn = get_db()
    try:
        conn.execute("DELETE FROM transactions")
        conn.execute("DELETE FROM stock")
        conn.execute("DELETE FROM products")
        conn.commit()

        flash("All inventory data has been deleted.", "success")
        return redirect(url_for("dashboard"))

    except sqlite3.Error:
        conn.rollback()
        flash("A database error occurred while clearing the data.", "error")
        return redirect(url_for("dashboard"))
    finally:
        conn.close()


# =========================================================
# START
# =========================================================

if __name__ == "__main__":
    init_db()

    print()
    print("======================================")
    print("       INVENTORY DASHBOARD")
    print("======================================")
    print()
    print("Open in browser:")
    print("http://127.0.0.1:5000")
    print()
    print("Pallets available: P01-P10")
    print("======================================")

    app.run(debug=True, use_reloader=False)
