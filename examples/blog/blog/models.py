from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models


class Author(models.Model):
    name = models.CharField(max_length=100)

    class Meta:
        ordering = ["name", "id"]

    def __str__(self):
        return self.name


class Tag(models.Model):
    name = models.SlugField(max_length=30, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Article(models.Model):
    author = models.ForeignKey(
        Author, related_name="articles", on_delete=models.CASCADE
    )
    tags = models.ManyToManyField(Tag, related_name="articles", blank=True)
    title = models.CharField(max_length=200)
    body = models.TextField()
    # The price of a paywalled article; 0.00 when it is free.
    price = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    published_at = models.DateTimeField()

    class Meta:
        ordering = ["-published_at", "id"]

    def __str__(self):
        return self.title
