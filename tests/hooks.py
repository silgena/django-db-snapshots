from tests.testapp.models import Customer


def rename_customers(path, models):
    Customer.objects.update(name="scrubbed")
