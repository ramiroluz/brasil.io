from copy import deepcopy
from pathlib import Path

from cached_property import cached_property
from django.contrib.auth import get_user_model
from django.contrib.postgres.fields import ArrayField, JSONField
from django.db import models
from django.urls import reverse
from localflavor.br.br_states import STATE_CHOICES

from covid19.exceptions import OnlyOneSpreadsheetException


def format_spreadsheet_name(instance, filename):
    # file will be uploaded to MEDIA_ROOT/{uf}/casos-{uf}-{date}-{username}-{file_no}.{extension}"
    # where {file_no} is the number of uploaded files from that user for the same pair of state
    # and date. this is necessary to avoid other users from overwriting other spreadsheets
    uf = instance.state.upper()
    date = instance.date.isoformat()
    user = instance.user.username
    file_no = StateSpreadsheet.objects.filter_older_versions(instance).count() + 1
    suffix = Path(filename).suffix
    return f"covid19/{uf}/casos-{uf}-{date}-{user}-{file_no}{suffix}"  # noqa


class StateSpreadsheetQuerySet(models.QuerySet):
    def filter_older_versions(self, spreadsheet):
        qs = self.from_user(spreadsheet.user).from_state(spreadsheet.state).filter(date=spreadsheet.date,)
        if spreadsheet.id:
            qs = qs.exclude(id=spreadsheet.id)
        return qs

    def from_user(self, user):
        return self.filter(user=user)

    def from_state(self, state):
        return self.filter(state__iexact=state)

    def cancel_older_versions(self, spreadsheet):
        return self.filter_older_versions(spreadsheet).update(cancelled=True)

    def filter_active(self):
        return self.filter(cancelled=False)

    def filter_inactive(self):
        return self.filter(cancelled=True)

    def deployed(self):
        return self.filter(status=self.model.DEPLOYED)

    def pending_review(self):
        return self.filter(status__in=[self.model.UPLOADED, self.model.CHECK_FAILED])

    def deployable_for_state(self, state, avoid_peer_review_dupes=True):
        qs = self.filter_active().from_state(state).deployed().order_by("-date")
        if avoid_peer_review_dupes:
            qs = qs.distinct("date")
        return qs


def default_data_json():
    return {
        "table": [],
        "errors": [],
        "warnings": [],
    }


class StateSpreadsheet(models.Model):
    UPLOADED, CHECK_FAILED, DEPLOYED = 1, 2, 3
    STATUS_CHOICES = (
        (UPLOADED, "uploaded"),
        (CHECK_FAILED, "check-failed"),
        (DEPLOYED, "deployed"),
    )

    objects = StateSpreadsheetQuerySet.as_manager()

    created_at = models.DateTimeField(auto_now_add=True)
    user = models.ForeignKey(get_user_model(), null=False, blank=False, on_delete=models.PROTECT)
    date = models.DateField(null=False, blank=False)
    state = models.CharField(max_length=2, null=False, blank=False, choices=STATE_CHOICES)
    file = models.FileField(upload_to=format_spreadsheet_name)
    peer_review = models.OneToOneField("self", null=True, blank=True, on_delete=models.SET_NULL)

    boletim_urls = ArrayField(models.TextField(), null=False, blank=False, help_text="Lista de URLs do(s) boletim(s)")
    boletim_notes = models.CharField(
        max_length=2000,
        default="",
        blank=True,
        help_text='Observações no boletim como "depois de publicar o boletim a secretaria postou no Twitter que teve mais uma morte".',  # noqa
    )

    # status da planilha: só aceitaremos planilhas sem erros, então quando ela
    # é subida, inicia-se um processo em background de checá-la conforme outra
    # planilha pro mesmo estado pra mesma data - esse worker é quem mudará o
    # status, o padrao qnd sobe a planilha e não tem erros é uploaded
    # (configurar celery ou rq)
    status = models.SmallIntegerField(choices=STATUS_CHOICES, default=UPLOADED)

    # dados da planilha depois de parseada no form, já em JSON, pro worker não
    # precisar ler o arquivo (o validador da planilha no form vai ter que fazer
    # essa leitura, então ele faz, se estiver tudo ok já salva nesse campo pro
    # worker já trabalhar com os dados limpos e normalizados)
    data = JSONField(default=default_data_json)

    # por padrao é False, mas vira True se um mesmo usuário subir uma planilha
    # pro mesmo estado pra mesma data (ele cancela o upload anterior pra essa
    # data/estado automaticamente caso suba uma atualizacao)
    cancelled = models.BooleanField(default=False)

    def __str__(self):
        active = "Ativa" if not self.cancelled else "Cancelada"
        return f"Planilha {active}: {self.state} - {self.date} por {self.user}"

    @property
    def active(self):
        return not self.cancelled

    @property
    def table_data(self):
        return deepcopy(self.data["table"])

    @table_data.setter
    def table_data(self, data):
        self.data["table"] = deepcopy(data)

    @property
    def warnings(self):
        return deepcopy(self.data["warnings"])

    @warnings.setter
    def warnings(self, data):
        self.data["warnings"] = data

    @property
    def errors(self):
        return deepcopy(self.data["errors"])

    @errors.setter
    def errors(self, data):
        self.data["errors"] = data
        self.status = StateSpreadsheet.CHECK_FAILED

    @cached_property
    def sibilings(self):
        qs = StateSpreadsheet.objects.filter_active().from_state(self.state).filter(date=self.date)
        return qs.pending_review().exclude(pk=self.pk, user_id=self.user_id).order_by("-created_at")

    @property
    def table_data_by_city(self):
        return {c["city"]: c for c in self.table_data if c["place_type"] == "city"}

    @property
    def ready_to_import(self):
        return all([self.status == StateSpreadsheet.UPLOADED, not self.cancelled, self.peer_review,])

    @property
    def admin_url(self):
        return reverse("admin:covid19_statespreadsheet_change", args=[self.pk])

    def get_data_from_city(self, ibge_code):
        if ibge_code:  # ibge_code = None match for undefined data
            ibge_code = int(ibge_code)
        try:
            return [d for d in self.table_data if d["city_ibge_code"] == ibge_code][0]
        except IndexError:
            return None

    def get_total_data(self):
        try:
            return [d for d in self.table_data if d["place_type"] == "state"][0]
        except IndexError:
            return None

    def compare_to_spreadsheet(self, other):
        match = lambda e1, e2: e1 and e2 and e1["deaths"] == e2["deaths"] and e1["confirmed"] == e2["confirmed"]  # noqa

        errors = []
        if not self.date == other.date:
            errors.append("Datas das planilhas são diferentes.")
        if not self.state == other.state:
            errors.append("Estados das planilhas são diferentes.")

        if errors:
            return errors

        user = self.user.username
        other_user = other.user.username
        self_cities = set(self.table_data_by_city.keys())
        other_cities = set(other.table_data_by_city.keys())

        extra_self_cities = self_cities - other_cities
        for extra_self in extra_self_cities:
            errors.append(
                f"{extra_self} está na planilha (por {user}) mas não na outra usada para a comparação (por {other_user})."  # noqa
            )
        extra_other_cities = other_cities - self_cities
        for extra_other in extra_other_cities:
            errors.append(
                f"{extra_other} está na planilha usada para a comparação (por {other_user}) mas não na importada (por {user}).",  # noqa
            )

        self_len = len(self.table_data)
        other_len = len(other.table_data)
        if not errors and self_len != other_len:
            errors.append(
                f"Número de entradas finais divergem. A planilha de comparação (por {other_user}) possui {other_len} mas a importada (por {user}) possui {self_len}.",  # noqa
            )

        for entry in self.table_data:
            display = entry["city"] or "Total"
            if entry["place_type"] == "state":
                other_entry = other.get_total_data()
            else:
                other_entry = other.get_data_from_city(entry["city_ibge_code"])

            if entry["city"] not in extra_self_cities and not match(entry, other_entry):
                errors.append(f"Número de casos confirmados ou óbitos diferem para {display}.")

        return errors

    def link_to_matching_spreadsheet_peer(self):
        """
        Compare the spreadsheet with the sibiling ones with the possible outputs:

        1. raise OnlyOneSpreadsheetException
        2. return a tuple as (True, [])
        3. return a tuple as (False, ['error 1', 'error 2'])
        """
        if not self.sibilings.exists():
            raise OnlyOneSpreadsheetException()

        errors = []
        for sibiling_spreadsheet in self.sibilings:
            errors = self.compare_to_spreadsheet(sibiling_spreadsheet)
            if not errors:
                self.link_to(sibiling_spreadsheet)
                sibiling_spreadsheet.link_to(self)
                return True, []
            else:
                sibiling_spreadsheet.errors = errors
                self.errors = errors
                sibiling_spreadsheet.save()
                self.save()

        return False, errors

    def link_to(self, other):
        self.peer_review = other
        self.errors = []
        self.status = StateSpreadsheet.UPLOADED
        self.save()

    def import_to_final_dataset(self, notification_callable=None):
        self.refresh_from_db()
        if not self.ready_to_import:
            raise ValueError("{self} is not ready to be imported")

        self.status = StateSpreadsheet.DEPLOYED
        self.save()
        self.peer_review.status = StateSpreadsheet.DEPLOYED
        self.peer_review.save()

        if notification_callable:
            notification_callable(self)
