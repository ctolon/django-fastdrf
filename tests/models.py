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


class Node(models.Model):
    """A tree: its serializer nests itself."""

    name = models.CharField(max_length=20)
    parent = models.ForeignKey(
        "self", null=True, related_name="children", on_delete=models.CASCADE
    )

    def __str__(self):
        return self.name


class Profile(models.Model):
    """A reverse one-to-one whose accessor and query name differ."""

    author = models.OneToOneField(
        Author,
        related_name="profile",
        related_query_name="profile_query",
        on_delete=models.CASCADE,
    )
    note = models.CharField(max_length=50)

    def __str__(self):
        return self.note


class Review(models.Model):
    """Reads columns of its edition through a foreign key that cannot be null."""

    edition = models.ForeignKey(Edition, on_delete=models.CASCADE)
    text = models.TextField(blank=True)

    def __str__(self):
        return self.text


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


class Handle:
    """A value object, as a package's model field returns one (a phone number)."""

    def __init__(self, raw):
        self.raw = raw

    def __str__(self):
        return f"@{self.raw}"

    def __eq__(self, other):
        return isinstance(other, Handle) and other.raw == self.raw

    __hash__ = None


class HandleDescriptor:
    """Wraps what is assigned, as django-phonenumber-field's descriptor does."""

    def __init__(self, field):
        self.field = field

    def __get__(self, instance, owner):
        if instance is None:
            return self
        return instance.__dict__[self.field.attname]

    def __set__(self, instance, value):
        if value is not None and not isinstance(value, Handle):
            value = Handle(value)
        instance.__dict__[self.field.attname] = value


class HandleField(models.CharField):
    """A model field of another package: its own descriptor and value class."""

    descriptor_class = HandleDescriptor

    def from_db_value(self, value, expression, connection):
        return None if value is None else Handle(value)

    def get_prep_value(self, value):
        if isinstance(value, Handle):
            value = value.raw
        return super().get_prep_value(value)


class Contact(models.Model):
    name = models.CharField(max_length=50)
    handle = HandleField(max_length=50, null=True)
    author = models.ForeignKey(Author, null=True, on_delete=models.SET_NULL)

    def __str__(self):
        return self.name


class Sku(str):
    """A stock-keeping unit: a code split into parts (the example of docs/extending.md)."""

    @property
    def parts(self):
        return self.split("-")


class SkuField(models.CharField):
    """Gives ``Sku`` objects for its column."""

    def from_db_value(self, value, expression, connection):
        return None if value is None else Sku(value)


class Product(models.Model):
    name = models.CharField(max_length=50)
    sku = SkuField(max_length=30)
    price = models.DecimalField(max_digits=8, decimal_places=2)

    def __str__(self):
        return self.name


class Code(models.Model):
    """A primary key of a field that gives its own objects (``Sku``)."""

    code = SkuField(primary_key=True, max_length=20)

    def __str__(self):
        return self.code


class Item(models.Model):
    code = models.ForeignKey(Code, on_delete=models.CASCADE)
    name = models.CharField(max_length=20)
    codes = models.ManyToManyField(Code, related_name="boxes")

    def __str__(self):
        return self.name


class Collision(models.Model):
    """A field named like the msgspec compiler's own names for shared sources."""

    number = models.IntegerField()
    _fastdrf_1 = models.IntegerField()
    _fastdrf_2 = models.IntegerField(default=0)

    def __str__(self):
        return str(self.number)
