"""Model fields and relations shared by compilation and queryset contracts."""

from django.db import models


class Author(models.Model):
    name = models.CharField(max_length=100)

    def __str__(self):
        return self.name


class Tag(models.Model):
    name = models.CharField(max_length=50, unique=True)

    def __str__(self):
        return self.name


class Book(models.Model):
    title = models.CharField(max_length=100)
    isbn = models.CharField(max_length=13, unique=True)
    pages = models.IntegerField(default=100)
    author = models.ForeignKey(Author, related_name="books", on_delete=models.CASCADE)
    tags = models.ManyToManyField(Tag, related_name="books", blank=True)
    contributors = models.ManyToManyField(Author, related_name="contributions")

    def __str__(self):
        return self.title


class Edition(models.Model):
    code = models.UUIDField()
    book = models.ForeignKey(Book, related_name="editions", on_delete=models.CASCADE)
    translator = models.ForeignKey(
        Author, null=True, blank=True, on_delete=models.SET_NULL
    )
    published = models.DateTimeField()
    released = models.DateField(null=True)
    active = models.BooleanField(default=True)
    rating = models.FloatField(null=True)
    price = models.DecimalField(max_digits=6, decimal_places=2)
    format = models.CharField(
        max_length=10, choices=[("hb", "Hardback"), ("pb", "Paperback")]
    )
    extra = models.JSONField(default=dict)
    notes = models.TextField(blank=True)

    def __str__(self):
        return str(self.code)


class Attachment(models.Model):
    """A file field: the compiled output builds its URL as DRF does."""

    title = models.CharField(max_length=100)
    file = models.FileField(upload_to="attachments/")

    def __str__(self):
        return self.title


class Shipment(models.Model):
    """A composite primary key (Django 5.2)."""

    pk = models.CompositePrimaryKey("carrier", "number")
    carrier = models.CharField(max_length=20)
    number = models.IntegerField()
    note = models.CharField(max_length=100, blank=True)

    def __str__(self):
        return f"{self.carrier} {self.number}"


class Invoice(models.Model):
    """Values the database computes: ``db_default`` and a ``GeneratedField``."""

    net = models.IntegerField()
    tax = models.IntegerField(db_default=5)
    total = models.GeneratedField(
        expression=models.F("net") + models.F("tax"),
        output_field=models.IntegerField(),
        db_persist=True,
    )

    def __str__(self):
        return f"invoice {self.pk}"


class Seat(models.Model):
    """A unique constraint with a condition: one open booking per seat number."""

    number = models.IntegerField()
    open = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["number"], condition=models.Q(open=True), name="one_open_seat"
            )
        ]

    def __str__(self):
        return f"seat {self.number}"
