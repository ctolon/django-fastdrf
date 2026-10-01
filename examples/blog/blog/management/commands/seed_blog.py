from django.core.management.base import BaseCommand

from blog.models import Article, Author, Tag
from blog.seed import seed


class Command(BaseCommand):
    help = "Replace the blog's data with the example's deterministic rows."

    def add_arguments(self, parser):
        parser.add_argument("--articles-per-author", type=int, default=30)

    def handle(self, *args, articles_per_author, **options):
        seed(articles_per_author)
        self.stdout.write(
            f"{Author.objects.count()} authors, {Tag.objects.count()} tags, "
            f"{Article.objects.count()} articles."
        )
