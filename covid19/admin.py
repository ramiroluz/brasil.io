import csv

from django.contrib import admin
from django.db import transaction
from django.http import Http404, HttpResponse
from django.templatetags.static import static
from django.urls import path
from django.utils.html import format_html

from brazil_data.cities import brazilian_cities_per_state
from brazil_data.states import STATES
from covid19.forms import StateSpreadsheetForm, state_choices_for_user
from covid19.models import StateSpreadsheet
from covid19.permissions import user_has_state_permission
from covid19.signals import new_spreadsheet_imported_signal
from covid19.spreadsheet_validator import TOTAL_LINE_DISPLAY, UNDEFINED_DISPLAY


class StateFilter(admin.SimpleListFilter):
    title = "state"
    parameter_name = "state"

    def lookups(self, request, model_admin):
        return [("", "Todos")] + state_choices_for_user(request.user)

    def queryset(self, request, queryset):
        state = self.value()
        if state:
            queryset = queryset.from_state(state)
        return queryset


class ActiveFilter(admin.SimpleListFilter):
    title = "active"
    parameter_name = "active"

    def lookups(self, request, model_admin):
        return [("True", "Ativa"), ("False", "Inativa")]

    def queryset(self, request, queryset):
        active = self.value()
        if active == "True":
            queryset = queryset.filter_active()
        elif active == "False":
            queryset = queryset.filter_inactive()
        return queryset


class StateSpreadsheetModelAdmin(admin.ModelAdmin):
    list_display = ["created_at", "state", "date", "user", "status", "warnings_list", "active"]
    list_filter = [StateFilter, "status", ActiveFilter]
    form = StateSpreadsheetForm
    ordering = ["-created_at"]
    add_form_template = "admin/covid19_add_form.html"

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "planilha-model/<str:state>/",
                self.admin_site.admin_view(self.sample_spreadsheet_view),
                name="sample_covid_spreadsheet",
            ),
        ]
        return custom_urls + urls

    def get_readonly_fields(self, request, obj=None):
        fields = []
        if obj:
            fields.extend(["created_at", "status", "active"])
            fields.extend(StateSpreadsheetForm.Meta.fields + ["warnings_list", "errors_list"])
        return fields

    def formfield_for_choice_field(self, db_field, request, **kwargs):
        if db_field.name == "state":
            kwargs["choices"] = state_choices_for_user(request.user)
        return super().formfield_for_choice_field(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change):
        is_new = not bool(obj.pk)

        obj.user = request.user
        if is_new:
            transaction.on_commit(lambda: new_spreadsheet_imported_signal.send(sender=self, spreadsheet=obj))
        super().save_model(request, obj, form, change)

    def get_queryset(self, request):
        qs = super().get_queryset(request).select_related("user")
        if not request.user.is_superuser:
            qs = qs.from_user(request.user)
        return qs

    def active(self, obj):
        images = {
            True: static("admin/img/icon-yes.svg"),
            False: static("admin/img/icon-no.svg"),
        }
        value = obj.active
        title = "Ativa" if value else "Inativa"
        return format_html(f'<img src="{images[value]}" title="{title}" alt="{title}">')

    def warnings_list(self, obj):
        li_tags = "".join([f"<li>{w}</li>" for w in obj.warnings])
        if not li_tags:
            return "---"
        else:
            return format_html(f"<ul>{li_tags}</ul>")

    warnings_list.short_description = "Warnings"

    def errors_list(self, obj):
        li_tags = "".join([f"<li>{w}</li>" for w in obj.errors])
        if not li_tags:
            return "---"
        else:
            return format_html(f"<ul>{li_tags}</ul>")

    errors_list.short_description = "Errors"

    def add_view(self, request, *args, **kwargs):
        extra_context = kwargs.get("extra_context") or {}

        allowed_states = []
        for state in STATES:
            if user_has_state_permission(request.user, state.acronym):
                allowed_states.append(state)

        extra_context["allowed_states"] = allowed_states
        kwargs["extra_context"] = extra_context
        return super().add_view(request, *args, **kwargs)

    def sample_spreadsheet_view(self, request, state):
        try:
            cities = brazilian_cities_per_state()[state]
        except KeyError:
            raise Http404

        if not user_has_state_permission(request.user, state):
            raise Http404

        csv_rows = [
            ("municipio", "confirmados", "mortes"),
            (TOTAL_LINE_DISPLAY, None, None),
            (UNDEFINED_DISPLAY, None, None),
        ] + [(city, None, None) for city in cities]

        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="modelo-{state}.csv"'

        writer = csv.writer(response)
        writer.writerows(csv_rows)

        return response


admin.site.register(StateSpreadsheet, StateSpreadsheetModelAdmin)
