from rest_framework.pagination import PageNumberPagination


class Paginacion(PageNumberPagination):
    """?page=N&page_size=M (por defecto 50, maximo 500)."""
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 500
