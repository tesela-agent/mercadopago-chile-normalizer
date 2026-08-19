#!/usr/bin/env python3
"""
Normalizador de cargos — la mitad del lector de ingresos que NO necesita secreto.

Qué es esto
-----------
Funciones puras: JSON del proveedor -> `Charge`. Sin red, sin disco, sin
credenciales, sin estado global. Nada acá importa `policy`, `chain` ni
`mpbroker`. Se puede leer entero y decidir si es correcto sin ejecutarlo — que
es justo lo que hace falta, porque yo no puedo ejecutarlo (ver README §"Sin
ejecución").

Por qué existe
--------------
`runtime/reconcile.py:58` hace `ch["amount"] / 100.0`. La propuesta 0002 §4.2 dijo
que el bug era "CLP no tiene unidad menor". Eso es cierto y es la mitad del
problema. La otra mitad, que es la que hace que el bug sea silencioso:

    Hay DOS ejes independientes y `/100.0` los colapsó en una constante.

      eje A — el exponente ISO 4217 de la moneda      (CLP 0, USD 2)
      eje B — la convención de monto DEL PROVEEDOR    (entero en unidades
              menores, o decimal en unidades mayores)

`/100.0` sólo acierta cuando A=2 y B=menores. Arreglar sólo el eje A deja el
lector roto para cualquier proveedor que reporte unidades mayores, y roto en la
dirección peligrosa: el número resultante sigue pareciendo un precio razonable.
Un error de 100x en CLP es visible; uno de 100x en un monto que ya venía en
unidades mayores es un dígito de más que nadie mira.

Qué es verificado y qué es recall
---------------------------------
Sólo una cosa acá está VERIFICADA (la llamé y leí los bytes, tick 6):
la tabla `MP_METHODS` y el hecho de que su clave es el PAR
`(payment_method_id, payment_type_id)` — `visa` y `master` aparecen dos veces
cada uno en Chile.

Todo lo demás sobre el formato de alambre es RECALL y está marcado así, aislado
en dos constantes con nombre: `CURRENCY_EXPONENT` y `AMOUNT_CONVENTION`. Si me
equivoqué, el arreglo es una constante, no un rediseño. Las predicciones y sus
falsadores están en
`agent/memory/decisions/2026-08-18-ingress-normalizer-against-fixtures.md`.

Nunca `float` para plata. `Decimal` en todos lados, y unidades menores enteras
como representación canónica.
"""
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import json
from typing import Any, Optional


# --------------------------------------------------------------------------
# Eje A — exponente de la moneda.
#
# PROVENIENCIA: ISO 4217, de memoria. RECALL, no verificado contra una fuente
# en este tick. La única entrada que hoy importa es CLP=0; las demás están para
# que el default no se aplique callado a una moneda que sí es de exponente cero.
# Si agregás una moneda, agregá también de dónde la sacaste.
# --------------------------------------------------------------------------
CURRENCY_EXPONENT: dict[str, int] = {
    "CLP": 0,   # Chile. El caso que importa hoy.
    "USD": 2,   # moneda del ledger y de POLICY.yaml
    "JPY": 0,
    "KRW": 0,
    "PYG": 0,
    "VND": 0,
    "ISK": 0,
}
DEFAULT_EXPONENT = 2

# Monedas para las que un monto fraccionario es imposible por definición.
# Sirve como falsador barato y automático: si llega CLP 1000.5, alguna de mis
# suposiciones sobre el formato de alambre es falsa. No detecta un error de
# escala (100000 y 1000 son ambos enteros) — eso sólo lo detecta un pago real de
# monto conocido de antemano. Ver README §"Lo que un test no puede atrapar".


# --------------------------------------------------------------------------
# Eje B — convención de monto del proveedor.
#
# RECALL. Predicciones P1 y P2 del archivo de decisión, con confianza 0.85 y
# 0.80. Una sola respuesta real de la API confirma o mata cada una.
#
#   "minor" -> entero en unidades menores. 1000 en USD = US$10.00.
#              Para monedas de exponente 0 el entero YA es la unidad mayor.
#   "major" -> número decimal en unidades mayores. 1000 en CLP = CLP 1000.
# --------------------------------------------------------------------------
AMOUNT_CONVENTION: dict[str, str] = {
    "stripe": "minor",        # P2, confianza 0.80
    "mercadopago": "major",   # P1, confianza 0.85
}


# --------------------------------------------------------------------------
# Tabla de métodos de MercadoPago Chile.
#
# VERIFICADO tick 6: GET https://api.mercadolibre.com/sites/MLC/payment_methods
# (sin autenticación, 2026-08-18). Diez entradas exactas.
#
# La clave es el PAR. `visa` y `master` aparecen cada uno dos veces bajo
# `payment_type_id` distinto. Una tabla indexada sólo por `payment_method_id`
# funde prepaga dentro de crédito sin decir nada. Ése es el trampolín exacto
# que este archivo existe para no pisar.
# --------------------------------------------------------------------------
MP_METHODS: dict[tuple[str, str], str] = {
    ("amex", "credit_card"): "American Express",
    ("master", "credit_card"): "Mastercard",
    ("visa", "credit_card"): "Visa",
    ("debvisa", "debit_card"): "Visa Débito",
    ("debmaster", "debit_card"): "Mastercard Débito",
    ("master", "prepaid_card"): "Mastercard Prepaid",
    ("visa", "prepaid_card"): "Visa Prepaid",
    ("account_money", "account_money"): "Dinero en cuenta MercadoPago",
    ("fintoc", "bank_transfer"): "Fintoc (transferencia bancaria)",
    ("consumer_credits", "digital_currency"): "Mercado Crédito",
}


class NormalizeError(ValueError):
    """El documento no se puede normalizar sin adivinar. Nunca devolvemos un
    Charge a medias: un Charge existe sólo si todos sus campos de plata son
    exactos."""


# --------------------------------------------------------------------------
# Plata
# --------------------------------------------------------------------------

def exponent(currency: str) -> int:
    return CURRENCY_EXPONENT.get((currency or "").upper(), DEFAULT_EXPONENT)


def to_decimal(value: Any, what: str = "monto") -> Decimal:
    """JSON -> Decimal sin pasar nunca por float.

    `Decimal(1000.1)` da 1000.099999999999909050529822707176208496093750.
    `Decimal("1000.1")` da 1000.1. La diferencia es plata mal contada, así que
    todo entra por str().
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool) or value is None:
        raise NormalizeError(f"{what}: valor no numérico {value!r}")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise NormalizeError(f"{what}: no es un número decimal: {value!r}")


def amount_to_major(raw: Any, currency: str, provider: str, what: str = "monto",
                    strict_integral: bool = True) -> Decimal:
    """El corazón del arreglo. Aplica los dos ejes, en orden, explícitamente.

    `strict_integral` sólo va en True para el monto COBRADO. Una comisión o un
    neto en CLP sí pueden ser fraccionarios — 3,19% de CLP 1000 es 31,9 — y
    reventar ahí voltearía un pago perfectamente válido. La integralidad es una
    propiedad del precio, no de la aritmética del proveedor sobre el precio.
    """
    conv = AMOUNT_CONVENTION.get(provider)
    if conv is None:
        raise NormalizeError(
            f"proveedor sin convención de monto declarada: {provider!r}. "
            f"Agregalo a AMOUNT_CONVENTION antes de leer su plata — "
            f"asumir una convención es exactamente el bug que este módulo arregla."
        )
    d = to_decimal(raw, what)
    if conv == "major":
        major = d
    elif conv == "minor":
        if d != d.to_integral_value():
            raise NormalizeError(
                f"{what}: {provider} declara unidades menores pero el valor "
                f"{raw!r} es fraccionario"
            )
        major = d.scaleb(-exponent(currency))
    else:
        raise NormalizeError(f"convención desconocida {conv!r} para {provider}")

    exp = exponent(currency)
    if strict_integral and exp == 0 and major != major.to_integral_value():
        # Falsador automático: CLP no tiene subdivisión. Si esto salta, alguna
        # suposición del eje A o del eje B es falsa. Preferible reventar acá que
        # asentar el número en el libro.
        raise NormalizeError(
            f"{what}: {currency} tiene exponente 0 pero el monto normalizado "
            f"es fraccionario ({major}). Revisá AMOUNT_CONVENTION[{provider!r}]."
        )
    return major


def to_minor_units(major: Decimal, currency: str) -> int:
    """Representación canónica entera. Rechaza pérdida de precisión."""
    exp = exponent(currency)
    shifted = major.scaleb(exp)
    if shifted != shifted.to_integral_value():
        raise NormalizeError(
            f"{major} {currency} no cabe en unidades menores sin redondear "
            f"(exponente {exp}). No redondeo plata en silencio."
        )
    return int(shifted)


# --------------------------------------------------------------------------
# El registro normalizado
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Charge:
    """Un cobro, en la forma en que el ledger lo puede usar.

    Regla de diseño (0002 §3): guardar hechos OBSERVADOS, nunca conclusiones
    derivadas. `net` es lo que el proveedor dijo que llegó, no `gross - fee`.
    Si difieren, lo anoto en `discrepancies` y no elijo por mi cuenta cuál gana.
    """
    provider: str
    provider_payment_id: str
    currency: str

    gross: Decimal                     # cobrado al pagador, unidades mayores
    fee: Optional[Decimal]             # comisión a mi cargo, como la reportó el proveedor
    net: Optional[Decimal]             # lo que efectivamente llega, como lo reportó el proveedor

    status: str                        # crudo, del proveedor
    status_detail: Optional[str]
    settled: bool                      # ¿es plata cobrada, no promesa?
    refunded: bool

    approved_at: Optional[str]         # ISO 8601 tal cual vino. Sin reinterpretar zona horaria.
    description: Optional[str]

    payer_email: Optional[str] = None
    payer_name: Optional[str] = None
    payer_tax_id: Optional[str] = None
    payer_country: Optional[str] = None
    payer_country_source: Optional[str] = None   # de qué campo salió, o None si no vino

    method_id: Optional[str] = None
    method_type: Optional[str] = None
    method_label: Optional[str] = None           # resuelto por el PAR, no por el id

    discrepancies: tuple[str, ...] = field(default_factory=tuple)

    @property
    def seen_key(self) -> str:
        """Clave de idempotencia. 0002 §5: `provider:id`, nunca el id pelado —
        dos proveedores colisionan en ids enteros y el segundo se descartaría
        como 'ya visto'."""
        return f"{self.provider}:{self.provider_payment_id}"

    @property
    def gross_minor(self) -> int:
        return to_minor_units(self.gross, self.currency)


def _check_net(gross: Decimal, fee: Optional[Decimal], net: Optional[Decimal]) -> tuple[str, ...]:
    """No corrige nada. Sólo deja constancia si los tres números no cierran.

    OJO — esto NO atrapa un error de escala: si el eje B está mal, gross, fee y
    net están todos mal por el mismo factor y la resta cierra igual. Ver README.
    """
    if fee is None or net is None:
        return ()
    if gross - fee != net:
        return (f"gross-fee={gross - fee} pero el proveedor reportó net={net}",)
    return ()


# --------------------------------------------------------------------------
# Stripe
# --------------------------------------------------------------------------

def normalize_stripe(ch: dict) -> Charge:
    """Stripe `charge`. Convención de monto: RECALL (P2)."""
    currency = (ch.get("currency") or "").upper()
    if not currency:
        raise NormalizeError("stripe: cargo sin `currency`")
    gross = amount_to_major(ch["amount"], currency, "stripe", "stripe.amount")

    refunded = bool(ch.get("refunded"))
    amt_ref = ch.get("amount_refunded")
    if not refunded and amt_ref:
        refunded = to_decimal(amt_ref, "stripe.amount_refunded") > 0

    return Charge(
        provider="stripe",
        provider_payment_id=str(ch["id"]),
        currency=currency,
        gross=gross,
        fee=None,       # vive en el balance_transaction, que es otra llamada. No lo invento.
        net=None,
        status=str(ch.get("status") or ""),
        status_detail=ch.get("failure_message"),
        settled=bool(ch.get("paid")) and not refunded,
        refunded=refunded,
        approved_at=ch.get("created") and str(ch["created"]),
        description=ch.get("description"),
        payer_email=ch.get("receipt_email"),
        payer_name=(ch.get("billing_details") or {}).get("name"),
        payer_country=((ch.get("billing_details") or {}).get("address") or {}).get("country"),
        payer_country_source="billing_details.address.country",
    )


# --------------------------------------------------------------------------
# MercadoPago
# --------------------------------------------------------------------------

# Sólo `approved` es plata cobrada. `pending`/`in_process` son promesas y
# `authorized` es una retención que todavía no se capturó. RECALL sobre el
# vocabulario exacto; el conjunto está acá y no disperso en ifs para que
# corregirlo sea una línea.
MP_SETTLED_STATUSES = {"approved"}
MP_REFUNDED_STATUSES = {"refunded", "charged_back"}

# Candidatos donde el país del pagador PODRÍA venir. P4 (confianza 0.60) dice
# que ninguno va a estar presente. Si es así, el país del pagador es un
# requisito de CHECKOUT y no un campo que se lee — y eso cambia el producto,
# no el lector (0002 §3).
_MP_COUNTRY_PATHS = (
    ("payer", "address", "country"),
    ("payer", "identification", "country"),
    ("additional_info", "payer", "address", "country"),
)


def _dig(doc: Any, path: tuple[str, ...]) -> Any:
    for k in path:
        if not isinstance(doc, dict):
            return None
        doc = doc.get(k)
    return doc


def _mp_fee(pay: dict) -> Optional[Decimal]:
    """Suma sólo las comisiones que paga el cobrador — o sea, yo.

    `fee_details` puede traer entradas con `fee_payer: "payer"` (las paga el
    comprador y no son un costo mío). Sumarlas todas sobreestima el costo y
    subestima el ingreso, que es errar en la dirección cómoda.
    """
    details = pay.get("fee_details")
    if not details:
        return None
    currency = (pay.get("currency_id") or "").upper()
    total = Decimal(0)
    seen_any = False
    for d in details:
        if not isinstance(d, dict) or "amount" not in d:
            continue
        if d.get("fee_payer") not in (None, "collector"):
            continue
        total += amount_to_major(d["amount"], currency, "mercadopago",
                                 "mp.fee_details[].amount", strict_integral=False)
        seen_any = True
    return total if seen_any else None


def normalize_mercadopago(pay: dict) -> Charge:
    """MercadoPago `payment`. Convención de monto: RECALL (P1)."""
    currency = (pay.get("currency_id") or "").upper()
    if not currency:
        raise NormalizeError("mercadopago: pago sin `currency_id`")

    gross = amount_to_major(pay["transaction_amount"], currency, "mercadopago",
                            "mp.transaction_amount")
    fee = _mp_fee(pay)

    td = pay.get("transaction_details") or {}
    net = None
    if td.get("net_received_amount") is not None:
        net = amount_to_major(td["net_received_amount"], currency, "mercadopago",
                              "mp.transaction_details.net_received_amount",
                              strict_integral=False)

    status = str(pay.get("status") or "")
    payer = pay.get("payer") or {}
    ident = payer.get("identification") or {}

    country, country_src = None, None
    for path in _MP_COUNTRY_PATHS:
        v = _dig(pay, path)
        if v:
            country, country_src = str(v), ".".join(path)
            break

    mid = pay.get("payment_method_id")
    mtype = pay.get("payment_type_id")
    label = MP_METHODS.get((mid, mtype)) if (mid and mtype) else None
    disc = list(_check_net(gross, fee, net))
    if mid and mtype and label is None:
        # No es un error: MercadoPago puede agregar métodos. Pero un método que
        # no reconozco no se debe registrar como si lo reconociera.
        disc.append(f"par de método desconocido para MLC: ({mid}, {mtype})")
    if mid and not mtype:
        disc.append(f"payment_method_id={mid!r} sin payment_type_id: el par es "
                    f"la clave, un id solo no identifica el instrumento")

    name = " ".join(x for x in (payer.get("first_name"), payer.get("last_name")) if x) or None

    return Charge(
        provider="mercadopago",
        provider_payment_id=str(pay["id"]),
        currency=currency,
        gross=gross,
        fee=fee,
        net=net,
        status=status,
        status_detail=pay.get("status_detail"),
        settled=status in MP_SETTLED_STATUSES,
        refunded=status in MP_REFUNDED_STATUSES,
        approved_at=pay.get("date_approved"),
        description=pay.get("description"),
        payer_email=payer.get("email"),
        payer_name=name,
        payer_tax_id=ident.get("number"),
        payer_country=country,
        payer_country_source=country_src,
        method_id=mid,
        method_type=mtype,
        method_label=label,
        discrepancies=tuple(disc),
    )


# --------------------------------------------------------------------------
# Anti-circularidad (§5)
# --------------------------------------------------------------------------

def circularity_hits(charge: Charge, watchlist) -> tuple[str, ...]:
    """0002 §5: el chequeo actual mira sólo `description`.

    En MercadoPago la descripción la escribo YO, así que un chequeo que sólo
    mire ahí examina un campo bajo mi control y no el que identifica a la
    contraparte. Mira el pagador.
    """
    fields = {
        "payer_email": charge.payer_email,
        "payer_name": charge.payer_name,
        "payer_tax_id": charge.payer_tax_id,
        "description": charge.description,
    }
    hits = []
    for w in watchlist or ():
        wl = str(w).lower()
        if not wl:
            continue
        for fname, val in fields.items():
            if val and wl in str(val).lower():
                hits.append(f"{fname}~{w}")
    return tuple(hits)


# --------------------------------------------------------------------------

NORMALIZERS = {
    "stripe": normalize_stripe,
    "mercadopago": normalize_mercadopago,
}


def normalize(provider: str, doc: dict) -> Charge:
    """0002 §5, defecto: un proveedor desconocido NO se saltea en silencio.

    `reconcile.py:49-51` hace `continue` y reporta ok:true. Un riel mal
    configurado queda indistinguible de un riel sin ventas — falla en la
    dirección de 'todo bien', que es la peor que este archivo puede tener.
    """
    fn = NORMALIZERS.get(provider)
    if fn is None:
        raise NormalizeError(
            f"proveedor desconocido: {provider!r}. No lo salteo: un proveedor "
            f"sin lector es una falla de configuración, no una ausencia de ventas."
        )
    return fn(doc)


def load_json(text: str) -> Any:
    """Parsea sin que ningún número de plata pase por float."""
    return json.loads(text, parse_float=Decimal)
