from django.conf import settings
from django.db import models


class Customer(models.Model):
    name = models.CharField(max_length=100)


class Tag(models.Model):
    name = models.CharField(max_length=50)


class Order(models.Model):
    customer = models.ForeignKey(Customer, models.CASCADE)
    placed_by = models.ForeignKey(settings.AUTH_USER_MODEL, models.SET_NULL, null=True)
    tags = models.ManyToManyField(Tag, blank=True)
    total = models.DecimalField(max_digits=8, decimal_places=2)


class Note(models.Model):
    """Not referenced by anything: dumping orders must not bring it along."""

    text = models.TextField()
