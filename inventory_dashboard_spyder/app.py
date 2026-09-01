from flask import Flask, render_template, request, redirect, url_for, flash
import sqlite3
from datetime import datetime, date

app = Flask(__name__)
app.secret_key = "inventory-dashboard-secret"

DB_NAME = "inventory_boxes.db"


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

    # =====================================================
    # PRODUCTS
    # =====================================================

    conn.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            box_weight REAL NOT NULL
        )
    """)

    # =====================================================
    # PALLETS
    # =====================================================

    conn.execute("""
        CREATE TABLE IF NOT EXISTS pallets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pallet_no INTEGER NOT NULL UNIQUE
        )
    """)

    # =====================================================
    # STOCK
    # =====================================================

    conn.execute("""
        CREATE TABLE IF NOT EXISTS stock (
            product_id INTEGER NOT NULL,
            pallet_id INTEGER NOT NULL,
            boxes REAL NOT NULL DEFAULT 0,

            PRIMARY KEY (product_id, pallet_id),

            FOREIGN KEY (product_id)
                REFERENCES products(id)
                ON DELETE CASCADE,

            FOREIGN KEY (pallet_id)
                REFERENCES pallets(id)
                ON DELETE CASCADE
        )
    """)

    # =====================================================
    # TRANSACTIONS
    # =====================================================

    conn.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            pallet_id INTEGER NOT NULL,
            movement_type TEXT NOT NULL,
            boxes REAL NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    # =====================================================
    # CHECK WHETHER transaction_date EXISTS
    # =====================================================

    columns = conn.execute(
        "PRAGMA table_info(transactions)"
    ).fetchall()

    column_names = [
        column["name"]
        for column in columns
    ]

    # =====================================================
    # ADD transaction_date TO OLD DATABASE
    # =====================================================

    if "transaction_date" not in column_names:

        conn.execute("""
            ALTER TABLE transactions
            ADD COLUMN transaction_date TEXT
        """)

        # Give old transactions today's date
        conn.execute("""
            UPDATE transactions
            SET transaction_date = ?
            WHERE transaction_date IS NULL
        """, (
            date.today().strftime("%Y-%m-%d"),
        ))

    # =====================================================
    # CREATE P01-P10
    # =====================================================

    for i in range(1, 11):

        conn.execute("""
            INSERT OR IGNORE INTO pallets
            (pallet_no)
            VALUES (?)
        """, (i,))

    conn.commit()
    conn.close()




# =========================================================
# DATES
# =========================================================

def get_transaction_dates():

    conn = get_db()

    rows = conn.execute("""
        SELECT DISTINCT transaction_date
        FROM transactions
        WHERE transaction_date IS NOT NULL
        ORDER BY transaction_date DESC
    """).fetchall()

    conn.close()

    return [row["transaction_date"] for row in rows]


def format_date(date_string):

    try:
        return datetime.strptime(
            date_string,
            "%Y-%m-%d"
        ).strftime("%d %B %Y")

    except:
        return date_string


# =========================================================
# DASHBOARD
# =========================================================

@app.route("/")
def dashboard():

    selected_date = request.args.get(
        "date",
        "total"
    )

    dates = get_transaction_dates()

    conn = get_db()

    # =====================================================
    # TOTAL / CURRENT STOCK
    # =====================================================

    if selected_date == "total":

        products = conn.execute("""
            SELECT

                p.id,
                p.name,
                p.box_weight,

                COALESCE(
                    SUM(
                        CASE
                            WHEN s.boxes > 0
                            THEN s.boxes
                            ELSE 0
                        END
                    ),
                    0
                ) AS total_boxes,

                COALESCE(
                    SUM(
                        CASE
                            WHEN s.boxes > 0
                            THEN s.boxes * p.box_weight
                            ELSE 0
                        END
                    ),
                    0
                ) AS total_weight,

                COUNT(
                    CASE
                        WHEN s.boxes > 0
                        THEN 1
                    END
                ) AS pallet_count

            FROM products p

            LEFT JOIN stock s
                ON p.id = s.product_id

            GROUP BY p.id

            HAVING total_boxes > 0

            ORDER BY p.name COLLATE NOCASE

        """).fetchall()


        total_stock = conn.execute("""
            SELECT
                COALESCE(
                    SUM(
                        s.boxes * p.box_weight
                    ),
                    0
                ) AS total

            FROM stock s

            JOIN products p
                ON p.id = s.product_id

            WHERE s.boxes > 0

        """).fetchone()["total"]


        total_boxes = conn.execute("""
            SELECT
                COALESCE(
                    SUM(boxes),
                    0
                ) AS total

            FROM stock

            WHERE boxes > 0

        """).fetchone()["total"]


        total_inward = conn.execute("""
            SELECT
                COALESCE(
                    SUM(
                        t.boxes * p.box_weight
                    ),
                    0
                ) AS total

            FROM transactions t

            JOIN products p
                ON p.id = t.product_id

            WHERE t.movement_type = 'Inward'

        """).fetchone()["total"]


        total_outward = conn.execute("""
            SELECT
                COALESCE(
                    SUM(
                        t.boxes * p.box_weight
                    ),
                    0
                ) AS total

            FROM transactions t

            JOIN products p
                ON p.id = t.product_id

            WHERE t.movement_type = 'Outward'

        """).fetchone()["total"]


        display_date = None


    # =====================================================
    # DATE-WISE STOCK
    # =====================================================

    else:

        try:

            datetime.strptime(
                selected_date,
                "%Y-%m-%d"
            )

        except ValueError:

            conn.close()

            return redirect(
                url_for("dashboard")
            )


        products_raw = conn.execute("""
            SELECT

                p.id,
                p.name,
                p.box_weight,

                COALESCE(
                    SUM(
                        CASE
                            WHEN t.movement_type = 'Inward'
                            THEN t.boxes

                            WHEN t.movement_type = 'Outward'
                            THEN -t.boxes

                            ELSE 0
                        END
                    ),
                    0
                ) AS total_boxes

            FROM products p

            LEFT JOIN transactions t
                ON p.id = t.product_id
                AND t.transaction_date <= ?

            GROUP BY p.id

            HAVING total_boxes > 0

            ORDER BY p.name COLLATE NOCASE

        """, (
            selected_date,
        )).fetchall()


        products = []


        for p in products_raw:

            # Find pallets containing this product
            # on the selected date

            pallet_rows = conn.execute("""
                SELECT

                    t.pallet_id,

                    SUM(
                        CASE
                            WHEN t.movement_type = 'Inward'
                            THEN t.boxes

                            WHEN t.movement_type = 'Outward'
                            THEN -t.boxes

                            ELSE 0
                        END
                    ) AS boxes

                FROM transactions t

                WHERE t.product_id = ?

                AND t.transaction_date <= ?

                GROUP BY t.pallet_id

                HAVING boxes > 0

            """, (
                p["id"],
                selected_date
            )).fetchall()


            total_boxes_product = p["total_boxes"]

            total_weight_product = (
                total_boxes_product *
                p["box_weight"]
            )


            products.append({

                "id": p["id"],

                "name": p["name"],

                "box_weight":
                    p["box_weight"],

                "total_boxes":
                    total_boxes_product,

                "total_weight":
                    total_weight_product,

                "pallet_count":
                    len(pallet_rows)

            })


        # Total stock on selected date

        total_stock = conn.execute("""
            SELECT

                COALESCE(
                    SUM(

                        CASE

                            WHEN t.movement_type = 'Inward'
                            THEN
                                t.boxes *
                                p.box_weight

                            WHEN t.movement_type = 'Outward'
                            THEN
                                -t.boxes *
                                p.box_weight

                            ELSE 0

                        END

                    ),
                    0

                ) AS total

            FROM transactions t

            JOIN products p
                ON p.id = t.product_id

            WHERE t.transaction_date <= ?

        """, (
            selected_date,
        )).fetchone()["total"]


        total_boxes = conn.execute("""
            SELECT

                COALESCE(
                    SUM(

                        CASE

                            WHEN movement_type = 'Inward'
                            THEN boxes

                            WHEN movement_type = 'Outward'
                            THEN -boxes

                            ELSE 0

                        END

                    ),
                    0

                ) AS total

            FROM transactions

            WHERE transaction_date <= ?

        """, (
            selected_date,
        )).fetchone()["total"]


        # Inward on this particular date

        total_inward = conn.execute("""
            SELECT

                COALESCE(
                    SUM(
                        t.boxes *
                        p.box_weight
                    ),
                    0
                ) AS total

            FROM transactions t

            JOIN products p
                ON p.id = t.product_id

            WHERE t.movement_type = 'Inward'

            AND t.transaction_date = ?

        """, (
            selected_date,
        )).fetchone()["total"]


        # Outward on this particular date

        total_outward = conn.execute("""
            SELECT

                COALESCE(
                    SUM(
                        t.boxes *
                        p.box_weight
                    ),
                    0
                ) AS total

            FROM transactions t

            JOIN products p
                ON p.id = t.product_id

            WHERE t.movement_type = 'Outward'

            AND t.transaction_date = ?

        """, (
            selected_date,
        )).fetchone()["total"]


        display_date = format_date(
            selected_date
        )


    conn.close()


    return render_template(

        "dashboard.html",

        products=products,

        total_stock=total_stock,

        total_boxes=total_boxes,

        total_inward=total_inward,

        total_outward=total_outward,

        dates=dates,

        selected_date=selected_date,

        display_date=display_date,

        today=date.today().strftime(
            "%Y-%m-%d"
        )

    )


# =========================================================
# ADD PRODUCT / STOCK
# =========================================================

@app.route(
    "/add_product",
    methods=["POST"]
)
def add_product():

    name = request.form.get(
        "name",
        ""
    ).strip()

    box_weight_text = request.form.get(
        "box_weight",
        ""
    ).strip()

    boxes_text = request.form.get(
        "boxes",
        ""
    ).strip()

    pallet_text = request.form.get(
        "pallet_no",
        ""
    ).strip()

    transaction_date = request.form.get(
        "transaction_date",
        ""
    ).strip()


    try:

        box_weight = float(
            box_weight_text
        )

        boxes = float(
            boxes_text
        )

        pallet_no = int(
            pallet_text
        )

        datetime.strptime(
            transaction_date,
            "%Y-%m-%d"
        )

    except:

        flash(
            "Please enter valid information.",
            "error"
        )

        return redirect(
            url_for("dashboard")
        )


    if not name:

        flash(
            "Please enter a product name.",
            "error"
        )

        return redirect(
            url_for("dashboard")
        )


    if box_weight <= 0 or boxes <= 0:

        flash(
            "Weight and quantity must be greater than zero.",
            "error"
        )

        return redirect(
            url_for("dashboard")
        )


    if pallet_no < 1 or pallet_no > 10:

        flash(
            "Please select a valid pallet.",
            "error"
        )

        return redirect(
            url_for("dashboard")
        )


    conn = get_db()


    # =====================================================
    # FIND PRODUCT
    # =====================================================

    existing = conn.execute("""
        SELECT *
        FROM products

        WHERE LOWER(name) = LOWER(?)

    """, (
        name,
    )).fetchone()


    if existing:

        product_id = existing["id"]

        # Keep the original product weight

        box_weight = existing["box_weight"]

        name = existing["name"]


    else:

        cursor = conn.execute("""
            INSERT INTO products
            (
                name,
                box_weight
            )

            VALUES (?, ?)

        """, (
            name,
            box_weight
        ))

        product_id = cursor.lastrowid


    # =====================================================
    # PALLET
    # =====================================================

    pallet = conn.execute("""
        SELECT *
        FROM pallets

        WHERE pallet_no = ?

    """, (
        pallet_no,
    )).fetchone()


    # =====================================================
    # CURRENT STOCK
    # =====================================================

    current = conn.execute("""
        SELECT boxes

        FROM stock

        WHERE product_id = ?

        AND pallet_id = ?

    """, (
        product_id,
        pallet["id"]
    )).fetchone()


    current_boxes = (

        current["boxes"]

        if current

        else 0

    )


    new_boxes = (
        current_boxes +
        boxes
    )


    # =====================================================
    # UPDATE STOCK
    # =====================================================

    conn.execute("""
        INSERT INTO stock
        (
            product_id,
            pallet_id,
            boxes
        )

        VALUES (?, ?, ?)

        ON CONFLICT(
            product_id,
            pallet_id
        )

        DO UPDATE SET

            boxes =
                excluded.boxes

    """, (
        product_id,
        pallet["id"],
        new_boxes
    ))


    # =====================================================
    # SAVE TRANSACTION
    # =====================================================

    conn.execute("""
        INSERT INTO transactions
        (
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
        "Inward",
        boxes,
        datetime.now().strftime(
            "%d-%m-%Y %I:%M %p"
        ),
        transaction_date
    ))


    conn.commit()
    conn.close()


    flash(
        f"{name}: {boxes:g} boxes "
        f"({boxes * box_weight:g} kg) "
        f"added to P{pallet_no:02d}.",
        "success"
    )


    return redirect(
        url_for("dashboard")
    )


# =========================================================
# UPDATE STOCK
# =========================================================

@app.route(
    "/update_stock",
    methods=["POST"]
)
def update_stock():

    product_id = request.form.get(
        "product_id"
    )

    pallet_no = request.form.get(
        "pallet_no"
    )

    movement_type = request.form.get(
        "movement_type"
    )

    boxes_text = request.form.get(
        "boxes"
    )

    transaction_date = request.form.get(
        "transaction_date"
    )


    try:

        product_id = int(
            product_id
        )

        pallet_no = int(
            pallet_no
        )

        boxes = float(
            boxes_text
        )

        datetime.strptime(
            transaction_date,
            "%Y-%m-%d"
        )

    except:

        flash(
            "Invalid stock information.",
            "error"
        )

        return redirect(
            request.referrer or
            url_for("dashboard")
        )


    conn = get_db()


    product = conn.execute("""
        SELECT *
        FROM products
        WHERE id = ?
    """, (
        product_id,
    )).fetchone()


    pallet = conn.execute("""
        SELECT *
        FROM pallets
        WHERE pallet_no = ?
    """, (
        pallet_no,
    )).fetchone()


    if not product or not pallet:

        conn.close()

        flash(
            "Product or pallet not found.",
            "error"
        )

        return redirect(
            url_for("dashboard")
        )


    current = conn.execute("""
        SELECT boxes

        FROM stock

        WHERE product_id = ?

        AND pallet_id = ?

    """, (
        product_id,
        pallet["id"]
    )).fetchone()


    current_boxes = (

        current["boxes"]

        if current

        else 0

    )


    if movement_type == "Outward":

        if boxes > current_boxes:

            conn.close()

            flash(
                "Not enough stock on this pallet.",
                "error"
            )

            return redirect(
                request.referrer or
                url_for("dashboard")
            )


        new_boxes = (
            current_boxes -
            boxes
        )


    else:

        new_boxes = (
            current_boxes +
            boxes
        )


    conn.execute("""
        INSERT INTO stock
        (
            product_id,
            pallet_id,
            boxes
        )

        VALUES (?, ?, ?)

        ON CONFLICT(
            product_id,
            pallet_id
        )

        DO UPDATE SET

            boxes =
                excluded.boxes

    """, (
        product_id,
        pallet["id"],
        new_boxes
    ))


    conn.execute("""
        INSERT INTO transactions
        (
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
        datetime.now().strftime(
            "%d-%m-%Y %I:%M %p"
        ),
        transaction_date
    ))


    conn.commit()
    conn.close()


    flash(
        f"{movement_type}: "
        f"{boxes:g} boxes "
        f"on P{pallet_no:02d}.",
        "success"
    )


    return redirect(
        request.referrer or
        url_for("dashboard")
    )


# =========================================================
# PRODUCT PAGE
# =========================================================

@app.route(
    "/product/<int:product_id>"
)
def product(product_id):

    conn = get_db()


    product = conn.execute("""
        SELECT *
        FROM products
        WHERE id = ?
    """, (
        product_id,
    )).fetchone()


    if not product:

        conn.close()

        return "Product not found", 404


    pallets = conn.execute("""
        SELECT

            pa.pallet_no,

            COALESCE(
                s.boxes,
                0
            ) AS boxes

        FROM pallets pa

        LEFT JOIN stock s

            ON pa.id = s.pallet_id

            AND s.product_id = ?

        ORDER BY pa.pallet_no

    """, (
        product_id,
    )).fetchall()


    total_boxes = sum(
        p["boxes"]
        for p in pallets
    )


    total_weight = (
        total_boxes *
        product["box_weight"]
    )


    history = conn.execute("""
        SELECT

            t.movement_type,
            t.boxes,
            t.created_at,
            t.transaction_date,
            pa.pallet_no

        FROM transactions t

        JOIN pallets pa
            ON pa.id = t.pallet_id

        WHERE t.product_id = ?

        ORDER BY
            t.transaction_date DESC,
            t.id DESC

    """, (
        product_id,
    )).fetchall()


    dates = get_transaction_dates()


    conn.close()


    return render_template(
        "product.html",

        product=product,

        pallets=pallets,

        total_boxes=total_boxes,

        total_weight=total_weight,

        history=history,

        dates=dates
    )


# =========================================================
# PALLET PAGE
# =========================================================

@app.route(
    "/pallet/<int:pallet_no>"
)
def pallet(pallet_no):

    if pallet_no < 1 or pallet_no > 10:

        return "Invalid pallet", 404


    conn = get_db()


    pallet = conn.execute("""
        SELECT *
        FROM pallets
        WHERE pallet_no = ?
    """, (
        pallet_no,
    )).fetchone()


    products = conn.execute("""
        SELECT

            p.id,
            p.name,
            p.box_weight,
            s.boxes,

            (
                s.boxes *
                p.box_weight
            ) AS weight

        FROM stock s

        JOIN products p
            ON p.id = s.product_id

        WHERE s.pallet_id = ?

        AND s.boxes > 0

        ORDER BY p.name COLLATE NOCASE

    """, (
        pallet["id"],
    )).fetchall()


    total_boxes = sum(
        p["boxes"]
        for p in products
    )


    total_weight = sum(
        p["weight"]
        for p in products
    )


    conn.close()


    return render_template(
        "pallet.html",

        pallet=pallet,

        products=products,

        total_boxes=total_boxes,

        total_weight=total_weight
    )


# =========================================================
# DELETE PRODUCT
# =========================================================

@app.route(
    "/delete_product/<int:product_id>",
    methods=["POST"]
)
def delete_product(product_id):

    conn = get_db()


    product = conn.execute("""
        SELECT *
        FROM products
        WHERE id = ?
    """, (
        product_id,
    )).fetchone()


    if not product:

        conn.close()

        flash(
            "Product not found.",
            "error"
        )

        return redirect(
            url_for("dashboard")
        )


    conn.execute("""
        DELETE FROM products
        WHERE id = ?
    """, (
        product_id,
    ))


    conn.commit()
    conn.close()


    flash(
        f"{product['name']} deleted.",
        "success"
    )


    return redirect(
        url_for("dashboard")
    )


# =========================================================
# CLEAR PALLET
# =========================================================

@app.route(
    "/clear_pallet",
    methods=["POST"]
)
def clear_pallet():

    pallet_no = request.form.get(
        "pallet_no"
    )


    try:

        pallet_no = int(
            pallet_no
        )

    except:

        flash(
            "Invalid pallet.",
            "error"
        )

        return redirect(
            url_for("dashboard")
        )


    conn = get_db()


    pallet = conn.execute("""
        SELECT *
        FROM pallets
        WHERE pallet_no = ?
    """, (
        pallet_no,
    )).fetchone()


    if not pallet:

        conn.close()

        flash(
            "Pallet not found.",
            "error"
        )

        return redirect(
            url_for("dashboard")
        )


    conn.execute("""
        DELETE FROM stock
        WHERE pallet_id = ?
    """, (
        pallet["id"],
    ))


    conn.execute("""
        DELETE FROM transactions
        WHERE pallet_id = ?
    """, (
        pallet["id"],
    ))


    conn.commit()
    conn.close()


    flash(
        f"P{pallet_no:02d} cleared.",
        "success"
    )


    return redirect(
        url_for("dashboard")
    )


# =========================================================
# CLEAR EVERYTHING
# =========================================================

@app.route(
    "/clear_all_data",
    methods=["POST"]
)
def clear_all_data():

    conn = get_db()

    conn.execute(
        "DELETE FROM transactions"
    )

    conn.execute(
        "DELETE FROM stock"
    )

    conn.execute(
        "DELETE FROM products"
    )

    conn.commit()
    conn.close()


    flash(
        "All inventory data deleted.",
        "success"
    )


    return redirect(
        url_for("dashboard")
    )


# =========================================================
# START APP
# =========================================================

if __name__ == "__main__":

    init_db()

    print()
    print("======================================")
    print("       INVENTORY DASHBOARD")
    print("======================================")
    print()
    print("Open in your browser:")
    print()
    print("http://127.0.0.1:5000")
    print()
    print("======================================")

    app.run(
        debug=True,
        use_reloader=False
    )