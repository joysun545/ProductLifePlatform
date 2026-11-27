from decimal import Decimal


def pricing_amounts(lines):
    """Separate confirmed product and accessory amounts without guessing old manual totals."""
    amounts = {}
    for field, kind in (("device_subtotal", "DEVICE"), ("accessory_subtotal", "ACCESSORY")):
        selected = [line.get("subtotal") for line in lines if line.get("kind", "DEVICE") == kind]
        amounts[field] = (
            format(sum((Decimal(value) for value in selected), Decimal(0)), ".2f")
            if lines and all(value is not None for value in selected) else None
        )
    return amounts
