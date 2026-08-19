#!/usr/bin/env python3
"""
Tests de `normalize.py`. Sin dependencias, sin red, sin secretos.

    python3 test_normalize.py

ESTADO: corrido por un humano el 2026-08-18 — 18/18 en verde. Yo no puedo
ejecutarlo (no tengo permiso de ejecución sobre código que yo escribo), así que
hasta ese momento este archivo fue una hipótesis sobre mi propio código y no
evidencia. Verde acá significa que el código hace lo que declaré; NO significa
que MercadoPago se comporte como asumí. Eso lo decide un pago real, no un test.

Los tests marcados [ASSUMPTION] no prueban que el proveedor se comporte así:
prueban que mi código implementa lo que declaré asumir. Se vuelven evidencia
recién cuando el fixture se reemplace por una respuesta real.
"""
import os
import sys
import json
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import normalize as N

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

_results = []


def check(name, fn):
    try:
        fn()
        _results.append((True, name, ""))
    except AssertionError as e:
        _results.append((False, name, str(e) or "assert"))
    except Exception as e:
        _results.append((False, name, f"{type(e).__name__}: {e}"))


def fixture(fname):
    with open(os.path.join(FIX, fname)) as f:
        return N.load_json(f.read())


def raises(exc, fn):
    try:
        fn()
    except exc:
        return
    raise AssertionError(f"esperaba {exc.__name__} y no ocurrió")


# ---------------------------------------------------------------- eje A

def t_exponents():
    assert N.exponent("CLP") == 0, "CLP no tiene unidad menor"
    assert N.exponent("clp") == 0, "debe ser insensible a mayúsculas"
    assert N.exponent("USD") == 2
    assert N.exponent("XYZ") == N.DEFAULT_EXPONENT


def t_minor_units_roundtrip():
    assert N.to_minor_units(Decimal("1000"), "CLP") == 1000
    assert N.to_minor_units(Decimal("10.00"), "USD") == 1000
    # No redondeo plata en silencio: 0.5 CLP no existe.
    raises(N.NormalizeError, lambda: N.to_minor_units(Decimal("0.5"), "CLP"))


# ------------------------------------------------- el bug de reconcile.py:58

def t_the_bug_itself():
    """El defecto, escrito como test antes que como parche.

    `runtime/reconcile.py:58` hace `ch["amount"] / 100.0`. Para un cargo de
    CLP 1000 asienta 10. Un error de 100x en el libro, en la dirección de
    'parece un precio razonable'.
    """
    def old_reconcile_py_58(amount):
        return amount / 100.0

    assert old_reconcile_py_58(1000) == 10.0, "así se comporta el código de hoy"

    ch = fixture("stripe_charge_clp.synthetic.json")
    got = N.normalize_stripe(ch)
    assert got.gross == Decimal("1000"), f"CLP 1000 debe librarse como 1000, no {got.gross}"
    assert got.gross != Decimal(str(old_reconcile_py_58(1000))), "el test no discrimina"


def t_stripe_usd_still_right():
    """El arreglo no debe romper el caso que hoy sí funciona. [ASSUMPTION P2]"""
    got = N.normalize_stripe(fixture("stripe_charge_usd.synthetic.json"))
    assert got.gross == Decimal("10.00"), got.gross
    assert got.currency == "USD"
    assert got.settled is True
    assert got.payer_country == "DE"


def t_never_float():
    """Plata en float es plata mal contada. Ni una sola vez."""
    for f, fn in (("stripe_charge_usd.synthetic.json", N.normalize_stripe),
                  ("mp_payment_clp_credit.synthetic.json", N.normalize_mercadopago)):
        c = fn(fixture(f))
        for attr in ("gross", "fee", "net"):
            v = getattr(c, attr)
            assert v is None or isinstance(v, Decimal), f"{f}.{attr} es {type(v)}"


def t_decimal_never_via_float():
    """Decimal(1000.1) != Decimal('1000.1'). El parser tiene que ir por str."""
    assert N.to_decimal(1000.1) == Decimal("1000.1")


# ------------------------------------------------------------- MercadoPago

def t_mp_amounts():
    """[ASSUMPTION P1] transaction_amount viene en unidades mayores."""
    c = N.normalize_mercadopago(fixture("mp_payment_clp_credit.synthetic.json"))
    assert c.currency == "CLP"
    assert c.gross == Decimal("1000"), c.gross
    assert c.fee == Decimal("31.9"), c.fee
    assert c.net == Decimal("968.1"), c.net
    assert c.discrepancies == (), c.discrepancies
    assert c.settled is True
    assert c.seen_key == "mercadopago:1234567890"


def t_mp_fee_may_be_fractional_in_clp():
    """CLP tiene exponente 0, pero 3,19% de 1000 es 31,9.

    La integralidad es propiedad del PRECIO, no de la aritmética del proveedor
    sobre el precio. Si esto revienta, `strict_integral` está mal puesto y un
    pago válido se cae entero.
    """
    c = N.normalize_mercadopago(fixture("mp_payment_clp_credit.synthetic.json"))
    assert c.fee == Decimal("31.9")


def t_the_pair_is_the_key():
    """VERIFICADO tick 6: en Chile `visa` aparece bajo credit_card Y prepaid_card.

    Una tabla indexada por payment_method_id solo funde prepaga en crédito sin
    decir nada. Este test es la única razón por la que ese bug no está acá.
    """
    credit = N.normalize_mercadopago(fixture("mp_payment_clp_credit.synthetic.json"))
    prepaid = N.normalize_mercadopago(fixture("mp_payment_clp_prepaid.synthetic.json"))

    assert credit.method_id == prepaid.method_id == "visa", "mismo id — ése es el punto"
    assert credit.method_label == "Visa", credit.method_label
    assert prepaid.method_label == "Visa Prepaid", prepaid.method_label
    assert credit.method_label != prepaid.method_label, \
        "el par (id, payment_type_id) no está discriminando"
    assert prepaid.discrepancies == (), prepaid.discrepancies


def t_unknown_method_pair_is_flagged_not_swallowed():
    doc = fixture("mp_payment_clp_credit.synthetic.json")
    doc["payment_method_id"] = "webpay"
    c = N.normalize_mercadopago(doc)
    assert c.method_label is None
    assert any("desconocido" in d for d in c.discrepancies), c.discrepancies


def t_fee_paid_by_payer_is_not_my_cost():
    doc = fixture("mp_payment_clp_credit.synthetic.json")
    doc["fee_details"] = [
        {"type": "mercadopago_fee", "amount": 31.9, "fee_payer": "collector"},
        {"type": "financing_fee", "amount": 500, "fee_payer": "payer"},
    ]
    c = N.normalize_mercadopago(doc)
    assert c.fee == Decimal("31.9"), \
        f"sumó una comisión que paga el comprador: {c.fee}"


def t_net_discrepancy_is_recorded_not_corrected():
    """0002 §3: guardar hechos observados, no conclusiones derivadas."""
    doc = fixture("mp_payment_clp_credit.synthetic.json")
    doc["transaction_details"]["net_received_amount"] = 900
    c = N.normalize_mercadopago(doc)
    assert c.net == Decimal("900"), "el neto observado se guarda tal cual"
    assert len(c.discrepancies) == 1, c.discrepancies


def t_pending_is_not_settled():
    doc = fixture("mp_payment_clp_credit.synthetic.json")
    doc["status"] = "in_process"
    assert N.normalize_mercadopago(doc).settled is False


def t_fractional_clp_gross_raises():
    """Falsador automático del eje B: CLP no se subdivide."""
    doc = fixture("mp_payment_clp_credit.synthetic.json")
    doc["transaction_amount"] = 1000.5
    raises(N.NormalizeError, lambda: N.normalize_mercadopago(doc))


# ----------------------------------------------------- circularidad y ruteo

def t_circularity_looks_at_the_payer():
    """0002 §5: hoy el chequeo mira sólo `description`, que la escribo YO."""
    c = N.normalize_mercadopago(fixture("mp_payment_clp_credit.synthetic.json"))
    hits = N.circularity_hits(c, ["testuser.com"])
    assert any(h.startswith("payer_email") for h in hits), hits
    assert N.circularity_hits(c, ["nadie@ninguna.parte"]) == ()


def t_unknown_provider_raises_instead_of_skipping():
    """`reconcile.py:49-51` hace `continue` y reporta ok:true. Un riel mal
    configurado no puede ser indistinguible de un riel sin ventas."""
    raises(N.NormalizeError, lambda: N.normalize("paypal", {}))


def t_seen_key_is_namespaced():
    a = N.normalize_stripe(fixture("stripe_charge_usd.synthetic.json"))
    b = N.normalize_mercadopago(fixture("mp_payment_clp_credit.synthetic.json"))
    assert a.seen_key != b.seen_key
    assert a.seen_key.startswith("stripe:") and b.seen_key.startswith("mercadopago:")


def t_provider_without_declared_convention_raises():
    """Nunca asumir la convención de monto de un proveedor nuevo: ése ES el bug."""
    raises(N.NormalizeError,
           lambda: N.amount_to_major(1000, "CLP", "proveedor_nuevo"))


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("t_")]


def main():
    for fn in TESTS:
        check(fn.__name__[2:], fn)
    failed = [r for r in _results if not r[0]]
    for ok, name, msg in _results:
        print(("  ok   " if ok else "  FAIL ") + name + (f"  -- {msg}" if msg else ""))
    print(f"\n{len(_results) - len(failed)}/{len(_results)} pasaron")
    if failed:
        print("\nUn test rojo acá es informacion, no vergüenza: dice cuál de las "
              "suposiciones marcadas [ASSUMPTION] es falsa.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
