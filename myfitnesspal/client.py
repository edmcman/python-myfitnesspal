from __future__ import annotations

import datetime
import json
import logging
import re
import uuid
from collections import OrderedDict
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any, cast, overload
from urllib import parse

import browser_cookie3
import lxml.html
import requests
from measurement.base import MeasureBase
from measurement.measures import Energy, Mass, Volume

from . import types
from .base import MFPBase
from .day import Day
from .entry import Entry
from .exceptions import MyfitnesspalLoginError, MyfitnesspalRequestFailed
from .exercise import Exercise
from .fooditem import FoodItem
from .meal import Meal
from .note import Note

logger = logging.getLogger(__name__)

BRITISH_UNIT_MATCHER = re.compile(r"(?:(?P<st>\d+) st)\W*(?:(?P<lbs>\d+) lb)?")


class Client(MFPBase):
    """Provides access to MyFitnessPal APIs"""

    COOKIE_DOMAINS = [
        "myfitnesspal.com",
        "www.myfitnesspal.com",
    ]
    BASE_URL = "http://www.myfitnesspal.com/"
    BASE_URL_SECURE = "https://www.myfitnesspal.com"
    BASE_API_URL = "https://api.myfitnesspal.com/"
    LOGIN_FORM_PATH = "account/login"
    LOGIN_JSON_PATH = "api/auth/callback/credentials"
    CSRF_PATH = "api/auth/csrf"
    SEARCH_PATH = "food/search"
    MEAL_MAP: dict[str, int] = {
        "breakfast": 0,
        "lunch": 1,
        "dinner": 2,
        "snacks": 3,
        "snack": 3,
    }
    ABBREVIATIONS = {
        "carbs": "carbohydrates",
    }
    DEFAULT_MEASURE_AND_UNIT = {
        "calories": (Energy, "Calorie"),
        "carbohydrates": (Mass, "g"),
        "fat": (Mass, "g"),
        "protein": (Mass, "g"),
        "sodium": (Mass, "mg"),
        "sugar": (Mass, "g"),
        "fiber": (Mass, "g"),
        "potass.": (Mass, "mg"),
        "kilojoules": (Energy, "kJ"),
    }

    def __init__(
        self,
        cookiejar: CookieJar | None = None,
        unit_aware: bool = False,
        log_requests_to: Path | None = None,
    ):
        self._client_instance_id = uuid.uuid4()
        self._request_counter = 0
        self._log_requests_to: Path | None = None
        if log_requests_to:
            self._log_requests_to = log_requests_to / Path(
                str(self._client_instance_id)
            )
            self._log_requests_to.mkdir(parents=True, exist_ok=True)

        self.unit_aware = unit_aware

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/88.0.4324.104 Safari/537.36"
            }
        )
        if cookiejar is not None:
            self.session.cookies.update(cookiejar)
        else:
            for domain_name in self.COOKIE_DOMAINS:
                self.session.cookies.update(
                    browser_cookie3.load(domain_name=domain_name)
                )

        self._auth_data = self._get_auth_data()
        self._user_metadata = self._get_user_metadata()

    @property
    def user_id(self) -> types.MyfitnesspalUserId | None:
        """The user_id of the logged-in account."""
        if self._auth_data is None:
            return None

        return self._auth_data["user_id"]

    @property
    def user_metadata(self) -> types.UserMetadata:
        """Metadata about of the logged-in account."""
        return self._user_metadata

    @property
    def access_token(self) -> str | None:
        """The access token for the logged-in account."""
        if self._auth_data is None:
            return None

        return self._auth_data["access_token"]

    @property
    def effective_username(self) -> str:
        """One's actual username may be different from the one used for login

        This method will return the actual username if it is available, but
        will fall back to the one provided if it is not.

        """
        return self.user_metadata["username"]

    def _get_auth_data(self) -> types.AuthData:
        result = self._get_request_for_url(
            parse.urljoin(self.BASE_URL_SECURE, "/user/auth_token") + "?refresh=true"
        )
        if not result.ok:
            raise MyfitnesspalRequestFailed(
                "Unable to fetch authentication token from MyFitnessPal: "
                "status code: {status}".format(status=result.status_code)
            )

        if not result.headers["Content-Type"].startswith("application/json"):
            # That we didn't receive a JSON document for this request
            # is the only obvious clear signal that we aren't logged-in.
            raise MyfitnesspalLoginError(
                "Could not access MyFitnessPal using the cookies provided "
                "by your browser.  Are you sure you have logged in to "
                "MyFitnessPal using a browser on this computer?"
            )

        return result.json()

    def _get_user_metadata(self) -> types.UserMetadata:
        requested_fields = [
            "diary_preferences",
            "goal_preferences",
            "unit_preferences",
            "paid_subscriptions",
            "account",
            "goal_displays",
            "location_preferences",
            "system_data",
            "profiles",
            "step_sources",
            "privacy_preferences",
            "social_preferences",
            "app_preferences",
            "partner_only_fields",
        ]
        query_string = parse.urlencode(
            [
                (
                    "fields[]",
                    name,
                )
                for name in requested_fields
            ]
        )
        metadata_url = (
            parse.urljoin(self.BASE_API_URL, f"/v2/users/{self.user_id}")
            + "?"
            + query_string
        )
        result = self._get_request_for_url(metadata_url, send_token=True)
        if not result.ok:
            logger.warning(
                "Unable to fetch user metadata; this may cause Myfitnesspal "
                "to behave incorrectly if you have logged-in with your "
                "e-mail address rather than your basic username; status %s.",
                result.status_code,
            )

        return result.json()["item"]

    def _get_full_name(self, raw_name: str) -> str:
        name = raw_name.lower().strip()
        if name not in self.ABBREVIATIONS:
            return name
        return self.ABBREVIATIONS[name]

    def _get_url_for_date(
        self, date: datetime.date, username: str, friend_username=None
    ) -> str:
        if friend_username is not None:
            name = friend_username
        else:
            name = username
        date_str = date.strftime("%Y-%m-%d")
        return (
            parse.urljoin(self.BASE_URL_SECURE, "food/diary/" + name)
            + f"?date={date_str}"
        )

    def _get_url_for_measurements(
        self, page: int = 1, measurement_name: str = ""
    ) -> str:
        return (
            parse.urljoin(self.BASE_URL_SECURE, "measurements/edit")
            + "?"
            + parse.urlencode({"page": page, "type": measurement_name})
        )

    def _get_request_for_url(
        self,
        url: str,
        send_token: bool = False,
        headers: dict[str, str] | None = None,
        **kwargs,
    ) -> requests.Response:
        request_id = uuid.uuid4()
        self._request_counter += 1
        logger.debug(
            "Sending request %s (#%s for client) to url %s",
            self._request_counter,
            request_id,
            url,
        )
        if headers is None:
            headers = {}

        if send_token:
            headers.update(
                {
                    "Authorization": f"Bearer {self.access_token}",
                    "mfp-client-id": "mfp-main-js",
                }
            )
            if self.user_id:
                headers["mfp-user-id"] = self.user_id

        result = self.session.get(url, headers=headers, **kwargs)
        if self._log_requests_to:
            with open(
                self._log_requests_to
                / Path(
                    str(self._request_counter).zfill(3) + "__" + str(request_id)
                ).with_suffix(".json"),
                "w",
                encoding="utf-8",
            ) as outf:
                outf.write(
                    json.dumps(
                        {
                            "request": {
                                "url": url,
                                "send_token": send_token,
                                "user_id": self.user_id if send_token else None,
                                "headers": headers,
                                "kwargs": kwargs,
                            },
                            "response": {
                                "headers": dict(result.headers),
                                "status_code": result.status_code,
                                "content": result.content.decode("utf-8"),
                            },
                        },
                        indent=4,
                        sort_keys=True,
                    )
                )

        return result

    def _get_content_for_url(self, *args, **kwargs) -> str:
        return self._get_request_for_url(*args, **kwargs).content.decode("utf8")

    def _get_document_for_url(self, url):
        content = self._get_content_for_url(url)

        return lxml.html.document_fromstring(content)

    def _get_json_for_url(self, url):
        content = self._get_content_for_url(url)

        return json.loads(content)

    def _get_measurement(self, name: str, value: float | None) -> MeasureBase:
        if not self.unit_aware:
            return value
        measure, kwarg = self.DEFAULT_MEASURE_AND_UNIT[name]
        return measure(**{kwarg: value})

    def _get_numeric(self, string: str) -> float:
        matched = BRITISH_UNIT_MATCHER.match(string)
        if matched:
            return float(matched.groupdict()["lbs"] or 0) + (
                float(matched.groupdict()["st"] or 0) * 14
            )
        else:
            try:
                str_value = re.sub(r"[^-\d.]+", "", string)
                return float(str_value)
            except ValueError:
                return 0

    def _get_fields(self, document):
        meal_header = document.xpath("//tr[@class='meal_header']")[0]
        tds = meal_header.findall("td")
        fields = ["name"]
        for field in tds[1:]:
            fields.append(self._get_full_name(field.text))
        return fields

    def _get_goals(self, document):
        try:
            total_header = document.xpath("//tr[@class='total']")[0]
        except IndexError:
            return None

        goal_header = total_header.getnext()  # The following TR contains goals
        columns = goal_header.findall("td")

        fields = self._get_fields(document)

        nutrition = {}
        for n in range(1, len(columns)):
            column = columns[n]
            try:
                nutr_name = fields[n]
            except IndexError:
                # This is the 'delete' button
                continue
            value = self._extract_value(column)
            nutrition[nutr_name] = self._get_measurement(nutr_name, value)

        return nutrition

    def _get_completion(self, document) -> bool:
        try:
            completion_header = document.xpath("//div[@id='complete_day']")[0]
            completion_message = completion_header.getchildren()[0]

            if "day_incomplete_message" in completion_message.classes:
                return False
            elif "day_complete_message" in completion_message.classes:
                return True
        except IndexError:
            pass

        return False  # Who knows, probably not my diary.

    def _get_meals(self, document) -> list[Meal]:
        meals = []
        fields = None
        meal_headers = document.xpath("//tr[@class='meal_header']")

        for meal_header in meal_headers:
            tds = meal_header.findall("td")
            meal_name = tds[0].text.lower()
            if fields is None:
                fields = self._get_fields(document)
            this = meal_header
            entries = []

            while True:
                this = this.getnext()
                if not this.attrib.get("class") is None:
                    break
                columns = this.findall("td")

                # When viewing a friend's diary, the HTML entries containing the
                # actual food log entries are different: they don't contain an
                # embedded <a/> element but rather the food name directly.
                if columns[0].find("a") is None:
                    name = columns[0].text.strip()
                else:
                    name = columns[0].find("a").text

                nutrition = {}

                for n in range(1, len(columns)):
                    column = columns[n]
                    try:
                        nutr_name = fields[n]
                    except IndexError:
                        # This is the 'delete' button
                        continue

                    value = self._extract_value(column)

                    nutrition[nutr_name] = self._get_measurement(nutr_name, value)

                entries.append(
                    Entry(
                        name,
                        nutrition,
                    )
                )

            meals.append(
                Meal(
                    meal_name,
                    entries,
                )
            )

        return meals

    def _get_url_for_exercise(self, date: datetime.date, username: str) -> str:
        date_str = date.strftime("%Y-%m-%d")
        return (
            parse.urljoin(self.BASE_URL_SECURE, "exercise/diary/" + username)
            + f"?date={date_str}"
        )

    def _get_exercise(self, document):
        exercises = []
        ex_headers = document.xpath("//table[@class='table0']")

        for ex_header in ex_headers:
            fields = []
            tds = ex_header.findall("thead")[0].findall("tr")[0].findall("td")
            ex_name = tds[0].text.lower()
            if len(fields) == 0:
                for field in tds:
                    fields.append(self._get_full_name(field.text))
            row = ex_header.findall("tbody")[0].findall("tr")[0]
            entries = []
            while True:
                if not row.attrib.get("class") is None:
                    break
                columns = row.findall("td")

                # Cardio diary exercise descriptions are anchor tags
                # within divs, but strength training exercise
                # descriptions are just anchor tags within the td.

                # But *first* we need to check whether an anchor
                # tag exists, or we throw an error looking for
                # an anchor tag within a div that doesn't exist

                # check for `td > a`
                name = ""
                if columns[0].find("a") is not None:
                    name = columns[0].find("a").text.strip()

                # If name is empty string:
                if columns[0].find("a") is None or not name:
                    # check for `td > div > a`
                    if columns[0].find("div").find("a") is None:
                        # then check for just `td > div`
                        # (this will occur when viewing a public diary entry)
                        if columns[0].find("div") is not None:
                            # if it exists, return `td > div.text`
                            name = columns[0].find("div").text.strip()
                        else:
                            # if neither, return `td.text`
                            name = columns[0].text.strip()
                    else:
                        # otherwise return `td > div > a.text`
                        name = columns[0].find("div").find("a").text.strip()

                attrs = {}

                for n in range(1, len(columns)):
                    column = columns[n]
                    try:
                        attr_name = fields[n]
                    except IndexError:
                        # This is the 'delete' button
                        continue

                    if column.text is None or "N/A" in column.text:
                        value = None
                    else:
                        value = self._get_numeric(column.text)

                    attrs[attr_name] = self._get_measurement(attr_name, value)

                entries.append(Entry(name, attrs))
                row = row.getnext()

            exercises.append(Exercise(ex_name, entries))

        return exercises

    def _get_exercises(self, date: datetime.date, friend_username=None):
        if friend_username is not None:
            name = friend_username
        else:
            name = self.effective_username
        # get the exercise URL
        document = self._get_document_for_url(self._get_url_for_exercise(date, name))
        # gather the exercise goals
        exercise = self._get_exercise(document)
        return exercise

    def _extract_value(self, element):
        if len(element.getchildren()) == 0:
            value = self._get_numeric(element.text)
        else:
            value = self._get_numeric(
                element.xpath("span[@class='macro-value']")[0].text
            )

        return value

    @overload
    def get_date(self, year: int, month: int, day: int) -> Day: ...

    @overload
    def get_date(self, date: datetime.date) -> Day: ...

    def get_date(self, *args, **kwargs) -> Day:
        """Returns your meal diary for a particular date"""
        if len(args) == 3:
            date = datetime.date(
                int(args[0]),
                int(args[1]),
                int(args[2]),
            )
        elif len(args) == 1 and isinstance(args[0], datetime.date):
            date = args[0]
        else:
            raise ValueError(
                "get_date accepts either a single datetime or date instance, "
                "or three integers representing year, month, and day "
                "respectively."
            )
        friend_username = kwargs.get("friend_username")
        document = self._get_document_for_url(
            self._get_url_for_date(
                date,
                kwargs.get("username", self.effective_username),
                friend_username,
            )
        )
        if "diary is locked with a key" in document.text_content():
            raise Exception("Error: diary is locked with a key")
        if (
            friend_username is not None
            and "user maintains a private diary" in document.text_content()
        ):
            raise Exception(
                f"Error: Friend {kwargs.get('friend_username')}'s diary is private."
            )

        meals = self._get_meals(document)
        goals = self._get_goals(document)
        complete = self._get_completion(document)

        # Since this data requires an additional request, let's just
        # allow the day object to run the request if necessary.
        notes = lambda: self._get_notes(date)  # noqa: E731
        water = lambda: self._get_water(date)  # noqa: E731
        exercises = lambda: self._get_exercises(date, friend_username)  # noqa: E731

        if "friend_username" not in kwargs:
            day = Day(
                date=date,
                meals=meals,
                goals=goals,
                notes=notes,
                water=water,
                exercises=exercises,
                complete=complete,
            )
        else:
            day = Day(
                date=date,
                meals=meals,
                goals=goals,
                exercises=exercises,
                complete=complete,
            )
        return day

    def _ensure_upper_lower_bound(self, lower_bound, upper_bound):
        if upper_bound is None:
            upper_bound = datetime.date.today()
        if lower_bound is None:
            lower_bound = upper_bound - datetime.timedelta(days=30)

        # If they entered the dates in the opposite order, let's
        # just flip them around for them as a convenience
        if lower_bound > upper_bound:
            lower_bound, upper_bound = upper_bound, lower_bound
        return upper_bound, lower_bound

    def get_measurements(
        self,
        measurement="Weight",
        lower_bound: datetime.date | None = None,
        upper_bound: datetime.date | None = None,
    ) -> dict[datetime.date, float]:
        """Returns measurements of a given name between two dates."""
        upper_bound, lower_bound = self._ensure_upper_lower_bound(
            lower_bound, upper_bound
        )

        # get the URL for the main check in page
        document = self._get_document_for_url(self._get_url_for_measurements())

        # gather the IDs for all measurement types
        measurement_ids = self._get_measurement_ids(document)

        if measurement not in measurement_ids.keys():
            raise ValueError(f"Measurement '{measurement}' does not exist.")

        page = 1
        measurements = OrderedDict()

        # retrieve entries until finished
        while True:
            # retrieve the HTML from MyFitnessPal
            document = self._get_document_for_url(
                self._get_url_for_measurements(page, measurement)
            )

            # parse the HTML for measurement entries and add to dictionary
            results = self._get_measurements(document)
            measurements.update(results)

            # stop if there are no more entries
            if len(results) == 0:
                break

            # continue if the lower bound has not been reached
            elif list(results.keys())[-1] > lower_bound:
                page += 1
                continue

            # otherwise stop
            else:
                break

        # remove entries that are not within the dates specified
        for date in list(measurements.keys()):
            if not upper_bound >= date >= lower_bound:
                del measurements[date]

        return measurements

    def set_measurements(
        self,
        measurement="Weight",
        value: float | None = None,
        date: datetime.date | None = None,
    ) -> None:
        """Sets measurement for today's date."""
        if value is None:
            raise ValueError("Cannot update blank value.")
        if date is None:
            date = datetime.datetime.now().date()
        if not isinstance(date, datetime.date):
            raise ValueError("Date must be a datetime.date object.")

        # get the URL for the main check in page
        # this is left in because we need to parse
        # the 'measurement' name to set the value.
        document = self._get_document_for_url(self._get_url_for_measurements())

        # gather the IDs for all measurement types
        measurement_ids = self._get_measurement_ids(document)

        authenticity_token = document.xpath(
            "(//form[@action='/measurements/new']/input[@name='authenticity_token']/@value)",
            smart_strings=False,
        )[0]

        # check if the measurement exists before going too far
        if measurement not in measurement_ids.keys():
            raise ValueError(f"Measurement '{measurement}' does not exist.")

        # build the update url.
        update_url = parse.urljoin(self.BASE_URL_SECURE, "measurements/new")

        # setup a dict for the post
        data = {
            "authenticity_token": authenticity_token,
            "measurement[display_value]": value,
            "type": measurement_ids.get(measurement),
            "measurement[entry_date(2i)]": date.month,
            "measurement[entry_date(3i)]": date.day,
            "measurement[entry_date(1i)]": date.year,
        }

        # now post it.
        result = self.session.post(update_url, data=data)

        # throw an error if it failed.
        if not result.ok:
            raise MyfitnesspalRequestFailed(
                "Unable to update measurement in MyFitnessPal: "
                "status code: {status}".format(status=result.status_code)
            )

    def _get_measurements(self, document):
        measurements = []

        for next_data in document.xpath("//script[@id='__NEXT_DATA__']"):
            next_data_json = json.loads(next_data.text)
            for q in next_data_json["props"]["pageProps"]["dehydratedState"]["queries"]:
                if "measurements" in q["queryKey"]:
                    if "items" in q["state"]["data"]:
                        measurements += q["state"]["data"]["items"]

        measurements_dict = OrderedDict()

        # converts the date to a datetime object and the value to a float
        for entry in measurements:
            date = datetime.datetime.strptime(entry["date"], "%Y-%m-%d").date()
            if "unit" in entry:
                value = f"{entry['value']} {entry['unit']}"
            else:
                value = f"{entry['value']}"
            measurements_dict[date] = self._get_numeric(value)

        return measurements_dict

    def _get_measurement_ids(self, document) -> dict[str, int]:
        ids = {}
        for next_data in document.xpath("//script[@id='__NEXT_DATA__']"):
            next_data_json = json.loads(next_data.text)
            for q in next_data_json["props"]["pageProps"]["dehydratedState"]["queries"]:
                if "measurementTypes" in q["queryKey"]:
                    for m in q["state"]["data"]:
                        ids[m["description"]] = m["id"]
                if "measurements" in q["queryKey"]:
                    if q["queryKey"][1] not in ids:
                        ids[q["queryKey"][1]] = ""

        return ids

    def _get_notes(self, date: datetime.date) -> Note:
        result = self._get_request_for_url(
            parse.urljoin(
                self.BASE_URL_SECURE,
                "/food/note",
            )
            + "?date={date}".format(date=date.strftime("%Y-%m-%d"))
        )
        return Note(result.json()["item"])

    def _get_water(self, date: datetime.date) -> float | Volume:
        result = self._get_request_for_url(
            parse.urljoin(
                self.BASE_URL_SECURE,
                "/food/water",
            )
            + "?date={date}".format(date=date.strftime("%Y-%m-%d"))
        )
        value = result.json()["item"]["milliliters"]
        if self.unit_aware:
            return Volume(ml=value)

        return value

    def set_water(self, date: datetime.date, milliliters: float) -> float:
        """Set water intake for a date.

        Returns the confirmed milliliter value from MFP.
        """
        search_url = parse.urljoin(self.BASE_URL_SECURE, self.SEARCH_PATH)
        doc = self._get_document_for_url(search_url)
        csrf_tokens = doc.xpath('//meta[@name="csrf-token"]/@content')
        if not csrf_tokens:
            raise MyfitnesspalRequestFailed(
                "Could not find CSRF token on food search page"
            )
        csrf_token = csrf_tokens[0]

        water_url = parse.urljoin(self.BASE_URL_SECURE, "food/water")
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "mfp-client-id": "mfp-main-js",
            "mfp-user-id": str(self.user_id),
            "Origin": self.BASE_URL_SECURE.rstrip("/"),
            "Referer": search_url,
            "X-CSRF-Token": csrf_token,
            "X-Requested-With": "XMLHttpRequest",
        }
        post_data = {
            "milliliters": str(milliliters),
            "date": date.strftime("%Y-%m-%d"),
        }
        result = self.session.post(water_url, data=post_data, headers=headers)
        if not result.ok:
            raise MyfitnesspalRequestFailed(
                f"Failed to set water: status code {result.status_code}, "
                f"response: {result.text[:200]}"
            )
        return result.json()["item"]["milliliters"]

    def get_report(
        self,
        report_name: str = "Net Calories",
        report_category: str = "Nutrition",
        lower_bound: datetime.date | None = None,
        upper_bound: datetime.date | None = None,
    ) -> dict[datetime.date, float]:
        """
        Returns report data of a given name and category between two dates.
        """
        if lower_bound and ((datetime.date.today() - lower_bound).days > 80):
            logger.warning(
                "Report API may not be able to look back this far. Some results may be incorrect."
            )

        upper_bound, lower_bound = self._ensure_upper_lower_bound(
            lower_bound, upper_bound
        )

        assert upper_bound
        assert lower_bound

        # Get the URL for the report
        json_data = self._get_json_for_url(
            self._get_url_for_report(report_name, report_category, lower_bound)
        )

        report = OrderedDict(self._get_report_data(json_data))

        if not report:
            raise ValueError("Could not load any results for the given category & name")

        # Remove entries that are not within the dates specified
        for date in list(report.keys()):
            if not upper_bound >= date >= lower_bound:
                del report[date]

        return report

    def _get_url_for_report(
        self, report_name: str, report_category: str, lower_bound: datetime.date
    ) -> str:
        delta = datetime.date.today() - lower_bound
        return (
            parse.urljoin(
                self.BASE_URL_SECURE,
                "api/services/reports/results/"
                + report_category.lower()
                + "/"
                + report_name,
            )
            + f"/{str(delta.days)}.json"
        )

    def _get_report_data(self, json_data: dict) -> dict[datetime.date, float]:
        report_data: dict[datetime.date, float] = {}

        data = json_data.get("outcome", {}).get("results")

        if not data:
            return report_data

        for index, entry in enumerate(data):
            # Dates are returned without year.
            # As the returned dates will always begin from the current day, the
            # correct date can be determined using the entry's index
            date = (
                datetime.date.today()
                - datetime.timedelta(days=len(data))
                + datetime.timedelta(days=index + 1)
            )

            report_data.update({date: entry["total"]})

        return report_data

    def __str__(self) -> str:
        return f"MyFitnessPal Client for {self.effective_username}"

    def get_food_search_results(self, query: str) -> list[FoodItem]:
        """Search for foods matching a specified query."""
        search_url = parse.urljoin(self.BASE_URL_SECURE, self.SEARCH_PATH)
        document = self._get_document_for_url(search_url)
        authenticity_token = document.xpath(
            "(//input[@name='authenticity_token']/@value)[1]"
        )[0]

        result = self.session.post(
            search_url,
            data={
                "authenticity_token": authenticity_token,
                "search": query,
                "date": datetime.datetime.today().strftime("%Y-%m-%d"),
                "meal": "0",
            },
        )

        # result.content is bytes so we decode it ASSUMING utf8 (which may be a
        # bad assumption?) PORTING_CHECK
        content = result.content.decode("utf8")
        document = lxml.html.document_fromstring(content)
        if "Matching Foods:" not in content:
            raise MyfitnesspalRequestFailed("Unable to load search results.")

        return self._get_food_search_results(document)

    def _get_food_search_results(self, document) -> list[FoodItem]:
        item_divs = document.xpath("//li[@class='matched-food']")

        items = []
        for item_div in item_divs:
            # get mfp info from search results
            a = item_div.xpath(".//div[@class='search-title-container']/a")[0]
            # Prefer the old-format ID (data-original-id) which works with /food/add;
            # fall back to data-external-id for foods that only exist in the new system.
            original_id_str = a.get("data-original-id")
            external_id_str = a.get("data-external-id")
            mfp_id = int(original_id_str) if original_id_str else int(external_id_str)
            external_id = int(external_id_str) if external_id_str else None
            weight_ids_str = a.get("data-weight-ids", "")
            old_weight_ids = [w for w in weight_ids_str.split(",") if w]
            mfp_name = a.text
            verif = (
                True
                if item_div.xpath(".//div[@class='verified verified-list-icon']")
                else False
            )
            calories = None
            brand = ""
            nutr_info_xpath = item_div.xpath(".//p[@class='search-nutritional-info']")
            if nutr_info_xpath:
                nutr_info = nutr_info_xpath[0].text.strip().split(",")
                if len(nutr_info) >= 3:
                    brand = " ".join(nutr_info[0:-2]).strip()
                calories = float(nutr_info[-1].replace("calories", "").strip())
            items.append(
                FoodItem(mfp_id, mfp_name, brand, verif, calories, client=self,
                         old_weight_ids=old_weight_ids, external_id=external_id)
            )

        return items

    def _get_food_item_details(self, mfp_id: int) -> types.FoodItemDetailsResponse:
        # api call for food item's details
        requested_fields = [
            "nutritional_contents",
            "serving_sizes",
            "confirmations",
        ]
        query_string = parse.urlencode(
            [
                (
                    "fields[]",
                    name,
                )
                for name in requested_fields
            ]
        )
        metadata_url = (
            parse.urljoin(self.BASE_API_URL, f"/v2/foods/{mfp_id}") + "?" + query_string
        )
        result = self._get_request_for_url(metadata_url, send_token=True)
        if not result.ok:
            raise MyfitnesspalRequestFailed(
                f"v2 food API returned {result.status_code} for food ID {mfp_id}"
            )

        resp = result.json()["item"]

        # identifying calories for default serving
        nutr_info = resp["nutritional_contents"]
        if "energy" in nutr_info:
            calories = nutr_info["energy"]["value"]
        else:
            calories = 0.0

        return {
            "description": resp["description"],
            "brand_name": resp.get("brand_name"),
            "verified": resp["verified"],
            "nutrition": nutr_info,
            "calories": calories,
            "confirmations": resp["confirmations"],
            "serving_sizes": resp["serving_sizes"],
        }

    def get_food_item_details(self, mfp_id: int) -> FoodItem:
        """Get details about a specific food using its ID."""
        details = self._get_food_item_details(mfp_id)

        # returning food item's details
        return FoodItem(
            mfp_id,
            details["description"],
            details["brand_name"],
            details["verified"],
            details["calories"],
            details=details["nutrition"],
            confirmations=details["confirmations"],
            serving_sizes=details["serving_sizes"],
            client=self,
        )

    def set_new_food(
        self,
        brand: str,
        description: str,
        calories: int,
        fat: float,
        carbs: float,
        protein: float,
        sodium: float | None = None,
        potassium: float | None = None,
        saturated_fat: float | None = None,
        polyunsaturated_fat: float | None = None,
        fiber: float | None = None,
        monounsaturated_fat: float | None = None,
        sugar: float | None = None,
        trans_fat: float | None = None,
        cholesterol: float | None = None,
        vitamin_a: float | None = None,
        calcium: float | None = None,
        vitamin_c: float | None = None,
        iron: float | None = None,
        serving_size: str = "1 Serving",
        servingspercontainer: float = 1.0,
        sharepublic: bool = False,
    ) -> None:
        """Function to submit new foods / groceries to the MyFitnessPal database. Function will return True if successful."""

        SUBMIT_PATH = "food/submit"
        SUBMIT_DUPLICATE_PATH = "food/duplicate"
        SUBMIT_NEW_PATH = (
            f"food/new?date={datetime.datetime.today().strftime('%Y-%m-%d')}&meal=0"
        )
        SUBMIT_POST_PATH = "food/new"

        # save current date in local variable for reusing
        date = datetime.datetime.today().strftime("%Y-%m-%d")

        # get Authenticity Token
        url = parse.urljoin(self.BASE_URL_SECURE, SUBMIT_PATH)
        document = self._get_document_for_url(url)
        authenticity_token = document.xpath(
            "(//input[@name='authenticity_token']/@value)[1]"
        )[0]
        utf8_field = document.xpath("(//input[@name='utf8']/@value)[1]")[0]

        # submit brand and description --> Possible returns duplicates warning
        url = parse.urljoin(self.BASE_URL_SECURE, SUBMIT_DUPLICATE_PATH)
        result = self.session.post(
            url,
            data={
                "utf8": utf8_field,
                "authenticity_token": authenticity_token,
                "date": date,
                "food[brand]": brand,
                "food[description]": description,
            },
        )
        if not result.ok:
            raise MyfitnesspalRequestFailed(
                f"Request Error - Unable to submit food to MyFitnessPal: status code: {result.status_code}"
            )

        # Check if a warning exists and log warning
        document = lxml.html.document_fromstring(result.content.decode("utf-8"))
        if document.xpath("//*[@id='main']/p[1]/span"):
            warning = document.xpath("//*[@id='main']/p[1]/span")[0].text
            logger.warning(f"My Fitness Pal responded: {warning}")
        # Passed Brand and Desc. Ready submit Form but needs new Authenticity Token
        url = parse.urljoin(self.BASE_URL_SECURE, SUBMIT_NEW_PATH)
        document = self._get_document_for_url(url)
        authenticity_token = document.xpath(
            "(//input[@name='authenticity_token']/@value)[1]"
        )[0]
        utf8_field = document.xpath("(//input[@name='utf8']/@value)[1]")[0]

        # Step4 - Build Post Data and finally submit new Food with nutritional Details
        data = {
            "utf8": utf8_field,
            "authenticity_token": authenticity_token,
            "date": date,
            "food[brand]": brand,
            "food[description]": description,
            "weight[serving_size]": serving_size,
            "servingspercontainer": f"{servingspercontainer}",
            "nutritional_content[calories]": f"{calories}",
            "nutritional_content[sodium]": f"{sodium or ''}",
            "nutritional_content[fat]": f"{fat}",
            "nutritional_content[potassium]": f"{potassium if potassium is not None else ''}",
            "nutritional_content[saturated_fat]": f"{saturated_fat if saturated_fat is not None else ''}",
            "nutritional_content[carbs]": f"{carbs}",
            "nutritional_content[polyunsaturated_fat]": f"{polyunsaturated_fat if polyunsaturated_fat is not None else ''}",
            "nutritional_content[fiber]": f"{fiber if fiber is not None else ''}",
            "nutritional_content[monounsaturated_fat]": f"{monounsaturated_fat if monounsaturated_fat is not None else ''}",
            "nutritional_content[sugar]": f"{sugar if sugar is not None else ''}",
            "nutritional_content[trans_fat]": f"{trans_fat if trans_fat is not None else ''}",
            "nutritional_content[protein]": f"{protein}",
            "nutritional_content[cholesterol]": f"{cholesterol if cholesterol is not None else ''}",
            "nutritional_content[vitamin_a]": f"{vitamin_a if vitamin_a is not None else ''}",
            "nutritional_content[calcium]": f"{calcium if calcium is not None else ''}",
            "nutritional_content[vitamin_c]": f"{vitamin_c if vitamin_c is not None else ''}",
            "nutritional_content[iron]": f"{iron if iron is not None else ''}",
            "food_entry[quantity]": "1.0",
            "food_entry[meal_id]": "0",
            "addtodiary": "no",
            "preserve_exact_description_and_brand": "true",
            "continue": "Save",
        }
        # Make entry public if requested, Hint: submit "sharefood": 0 also generates a public db entry, so only add
        # "sharefood"" if really requested
        if sharepublic:
            data["sharefood"] = 1

        url = parse.urljoin(self.BASE_URL_SECURE, SUBMIT_POST_PATH)
        result = self.session.post(
            url,
            data,
        )
        if not result.ok:
            raise MyfitnesspalRequestFailed(
                f"Request Error - Unable to submit food to MyFitnessPal: status code: {result.status_code}"
            )

        document = lxml.html.document_fromstring(result.content.decode("utf-8"))

        if document.xpath(
            # If list is empty there should be no error, could be replaced with assert
            "//*[@id='errorExplanation']/ul/li"
        ):
            error = document.xpath("//*[@id='errorExplanation']/ul/li")[0].text
            error = error.replace("Description ", "")  # For cosmetic reasons
            raise MyfitnesspalRequestFailed(
                f"Unable to submit food to MyFitnessPal: {error}"
            )

        # Would like to return FoodItem, but seems that it take
        # to long until the submitted food is available in the DB
        # return self.get_food_search_results("{} {}".format(brand, description))[0]

    def set_new_goal(
        self,
        energy: float,
        energy_unit: str = "calories",
        carbohydrates: float | None = None,
        protein: float | None = None,
        fat: float | None = None,
        percent_carbohydrates: float | None = None,
        percent_protein: float | None = None,
        percent_fat: float | None = None,
    ) -> None:
        """Updates your nutrition goals.

        This Function will update your nutrition goals and is able to deal with multiple situations based on the passed arguments.
        First matching situation will be applied and used to update the nutrition goals.

        Passed arguments - Hints:
        energy and all absolute macro values - Energy value will be adjusted/calculated if energy from macro values is higher than provided energy value.
        energy and all percentage macro values - Energy will be adjusted and split into macros by provided percentage.
        energy - Energy will be adjusted and split into macros by percentage as before.

        Optional arguments:
        energy_unit - Function is able to deal with calories and kilojoules. If not provided user preferences will be used.

        Additional hints:
        Values will be adjusted and rounded by MFP if no premium subscription is applied!
        """
        # FROM MFP JS:
        # var calculated_energy = 4 * parseFloat(this.get('carbGrams')) + 4 * parseFloat(this.get('proteinGrams')) + 9 * parseFloat(this.get('fatsGrams'));

        # Get User Default Unit Preference
        if energy_unit != "calories" and energy_unit != "kilojoules":
            assert self.user_metadata
            energy_unit = self.user_metadata["unit_preferences"]["energy"]

        # Get authenticity token and current values
        url = parse.urljoin(self.BASE_URL_SECURE, "account/my_goals")
        today = datetime.datetime.now().strftime("%Y-%m-%d")

        # Build header for API-requests
        auth_header = self.session.headers
        auth_header["authorization"] = f"Bearer {self.access_token}"
        auth_header["mfp-client-id"] = "mfp-main-js"
        auth_header["mfp-user-id"] = f"{self.user_id}"

        # Get Request for old goal values
        old_goals_url = parse.urljoin(
            self.BASE_API_URL, f"v2/nutrient-goals?date={today}"
        )
        old_goals_document = self.session.get(old_goals_url, headers=auth_header)
        old_goals = json.loads(old_goals_document.text)

        # Marcro Calculation
        # If no macro goals were provided calculate them with percentage value
        if carbohydrates is None or protein is None or fat is None:
            # If even no macro percentages values were provided calculate them from old values
            if (
                percent_carbohydrates is None
                or percent_protein is None
                or percent_fat is None
            ):
                old_energy_value = old_goals["items"][0]["default_goal"]["energy"][
                    "value"
                ]
                old_energy_unit = old_goals["items"][0]["default_goal"]["energy"][
                    "unit"
                ]
                old_carbohydrates = old_goals["items"][0]["default_goal"][
                    "carbohydrates"
                ]
                old_fat = old_goals["items"][0]["default_goal"]["fat"]
                old_protein = old_goals["items"][0]["default_goal"]["protein"]

                # If old and new values are in diffrent units then convert old value to new unit
                if not old_energy_unit == energy_unit:
                    if old_energy_unit not in ["kilojoules", "calories"]:
                        raise Exception(
                            f"Unexpected energy unit in historical goals: {old_energy_unit}"
                        )
                    if energy_unit not in ["kilojoules", "calories"]:
                        raise ValueError(
                            f"Unexpected energy unit in goals: {energy_unit}"
                        )

                    if old_energy_unit == "kilojoules" and energy_unit == "calories":
                        old_energy_value *= 0.2388
                        old_energy_unit = "calories"
                    elif old_energy_unit == "calories" and energy_unit == "kilojoules":
                        """FROM MFP JS
                        if (energyUnit === 'kilojoules') {
                            calories *= 4.184;
                        }
                        """
                        old_energy_value *= 4.184
                        old_energy_unit = "kilojoules"

                carbohydrates = energy * old_carbohydrates / old_energy_value
                protein = energy * old_protein / old_energy_value
                fat = energy * old_fat / old_energy_value
            # If percentage values were provided check
            else:
                if int(percent_carbohydrates + percent_protein + percent_fat) != 100:
                    raise ValueError("Provided percentage values do not add to 100%.")

                carbohydrates = energy * percent_carbohydrates / 100.0 / 4
                protein = energy * percent_protein / 100.0 / 4
                fat = energy * percent_fat / 100.0 / 9
                if energy_unit == "kilojoules":
                    carbohydrates = round(carbohydrates / 4.184, 2)
                    protein = round(protein / 4.184, 2)
                    fat = round(fat / 4.184, 2)
        else:
            macro_energy = carbohydrates * 4 + protein * 4 + fat * 9
            if energy_unit == "kilojoules":
                macro_energy *= 4.184
            # Compare energy values and set it correctly due to macros. Will also fix if no energy_value was provided.
            if energy < macro_energy:
                logger.warning(
                    "Provided energy value and calculated energy value from macros do not match! Will override!"
                )
                energy = macro_energy

        # Build payload based on observed browser behaviour
        new_goals = {}
        new_goals["item"] = old_goals["items"][0]
        new_goals["item"].pop("valid_to", None)
        new_goals["item"].pop("default_group_id", None)
        new_goals["item"].pop("updated_at", None)
        new_goals["item"]["default_goal"]["meal_goals"] = []

        # insert new values
        new_goals["item"]["valid_from"] = today

        new_goals["item"]["default_goal"]["energy"]["value"] = energy
        new_goals["item"]["default_goal"]["energy"]["unit"] = energy_unit
        new_goals["item"]["default_goal"]["carbohydrates"] = carbohydrates
        new_goals["item"]["default_goal"]["protein"] = protein
        new_goals["item"]["default_goal"]["fat"] = fat

        for goal in new_goals["item"]["daily_goals"]:
            goal["meal_goals"] = []
            goal.pop("group_id", None)

            goal["energy"]["value"] = energy
            goal["energy"]["unit"] = energy_unit
            goal["carbohydrates"] = carbohydrates
            goal["protein"] = protein
            goal["fat"] = fat

        # Build request and post
        url = parse.urljoin(self.BASE_API_URL, "v2/nutrient-goals")
        result = self.session.post(url, json.dumps(new_goals), headers=auth_header)

        if not result.ok:
            raise MyfitnesspalRequestFailed(
                "Request Error - Unable to submit Goals to MyFitnessPal: "
                "status code: {status}".format(status=result.status_code)
            )

    def get_recipes(self) -> dict[int, str]:
        """Returns a dictionary with all saved recipes.

        Recipe ID will be used as dictionary key, recipe title as dictionary value.
        """
        recipes_dict = {}

        page_count = 1
        has_next_page = True
        while has_next_page:
            RECIPES_PATH = f"recipe_parser?page={page_count}&sort_order=recent"
            recipes_url = parse.urljoin(self.BASE_URL_SECURE, RECIPES_PATH)
            document = self._get_document_for_url(recipes_url)
            recipes = document.xpath(
                "//*[@id='main']/ul[1]/li"
            )  # get all items in the recipe list
            for recipe_info in recipes:
                recipe_path = recipe_info.xpath("./div[2]/h2/span[1]/a")[0].attrib[
                    "href"
                ]
                recipe_id = recipe_path.split("/")[-1]
                recipe_title = recipe_info.xpath("./div[2]/h2/span[1]/a")[0].attrib[
                    "title"
                ]
                recipes_dict[recipe_id] = recipe_title

            # Check for Pagination
            pagination_links = document.xpath('//*[@id="main"]/ul[2]/a')
            if pagination_links:
                if page_count == 1:
                    # If Pagination exists and it is page 1 there have to be a second,
                    # but only one href to the next (obviously none to the previous)
                    page_count += 1
                elif len(pagination_links) > 1:
                    # If there are two links, ont to the previous and one to the next
                    page_count += 1
                else:
                    # Only one link means it is the last page
                    has_next_page = False
            else:
                # Indicator for no recipes if len(recipes_dict) is 0 here
                has_next_page = False

        return recipes_dict

    def get_recipe(self, recipeid: int) -> types.Recipe:
        """Returns recipe details in a dictionary.

        See https://schema.org/Recipe for details regarding this schema.
        """
        recipe_path = f"/recipe/view/{recipeid}"
        recipe_url = parse.urljoin(self.BASE_URL_SECURE, recipe_path)
        document = self._get_document_for_url(recipe_url)

        recipe_dict: dict[str, Any] = {
            "@context": "https://schema.org",
            "@type": "Recipe",
            "author": self.effective_username,
        }
        recipe_dict["org_url"] = recipe_url
        recipe_dict["name"] = document.xpath('//*[@id="main"]/div[3]/div[2]/h1')[0].text
        recipe_dict["recipeYield"] = document.xpath('//*[@id="recipe_servings"]')[
            0
        ].text

        recipe_dict["recipeIngredient"] = []
        ingredients = document.xpath('//*[@id="main"]/div[4]/div/*/li')
        for ingredient in ingredients:
            recipe_dict["recipeIngredient"].append(ingredient.text.strip(" \n"))

        recipe_dict["nutrition"] = {"@type": "NutritionInformation"}
        recipe_dict["nutrition"]["calories"] = document.xpath(
            '//*[@id="main"]/div[3]/div[2]/div[2]/div'
        )[0].text.strip(" \n")
        recipe_dict["nutrition"]["carbohydrateContent"] = document.xpath(
            '//*[@id="carbs"]/td[1]/span[2]'
        )[0].text.strip(" \n")
        recipe_dict["nutrition"]["fiberContent"] = document.xpath(
            '//*[@id="fiber"]/td[1]/span[2]'
        )[0].text.strip(" \n")
        recipe_dict["nutrition"]["sugarContent"] = document.xpath(
            '//*[@id="sugar"]/td[1]/span[2]'
        )[0].text.strip(" \n")
        recipe_dict["nutrition"]["sodiumContent"] = document.xpath(
            '//*[@id="sodium"]/td[1]/span[2]'
        )[0].text.strip(" \n")
        recipe_dict["nutrition"]["proteinContent"] = document.xpath(
            '//*[@id="protein"]/td[1]/span[2]'
        )[0].text.strip(" \n")
        recipe_dict["nutrition"]["fatContent"] = document.xpath(
            '//*[@id="total_fat"]/td[1]/span[2]'
        )[0].text.strip(" \n")
        recipe_dict["nutrition"]["saturatedFatContent"] = document.xpath(
            '//*[@id="saturated_fat"]/td[1]/span[2]'
        )[0].text.strip(" \n")
        recipe_dict["nutrition"]["monounsaturatedFatContent"] = document.xpath(
            '//*[@id="monounsaturated_fat"]/td[1]/span[2]'
        )[0].text.strip(" \n")
        recipe_dict["nutrition"]["polyunsaturatedFatContent"] = document.xpath(
            '//*[@id="polyunsaturated_fat"]/td[1]/span[2]'
        )[0].text.strip(" \n")
        recipe_dict["nutrition"]["unsaturatedFatContent"] = int(
            recipe_dict["nutrition"]["polyunsaturatedFatContent"]
        ) + int(recipe_dict["nutrition"]["monounsaturatedFatContent"])
        recipe_dict["nutrition"]["transFatContent"] = document.xpath(
            '//*[@id="trans_fat"]/td[1]/span[2]'
        )[0].text.strip(" \n")

        # add some required tags to match schema
        recipe_dict["recipeInstructions"] = ""
        recipe_dict["tags"] = ["MyFitnessPal"]
        return cast(types.Recipe, recipe_dict)

    def get_meals(self) -> dict[int, str]:
        """Returns a dictionary with all saved meals.

        Key: Meal ID (int)
        Value: Meal Name

        Uses the JSON API because the meal/mine page migrated to Next.js and
        the old XPath-based scraping no longer works.
        """
        meals_url = parse.urljoin(
            self.BASE_URL_SECURE, "api/services/users/meals/mine"
        )
        result = self._get_request_for_url(meals_url)
        if not result.ok:
            logger.warning("Failed to fetch meals: HTTP %s", result.status_code)
            return {}
        return {int(meal["meal_id"]): meal["description"] for meal in result.json()}

    def get_meals_detailed(self) -> list[dict[str, Any]]:
        """Returns full meal data from the JSON API, including ingredient lists.

        Each element has keys: meal_id (int), description (str), foods (list).
        Each food has: description, calories, carbs, fat, protein, sodium, sugar.
        """
        meals_url = parse.urljoin(
            self.BASE_URL_SECURE, "api/services/users/meals/mine"
        )
        result = self._get_request_for_url(meals_url)
        if not result.ok:
            logger.warning("Failed to fetch meals: HTTP %s", result.status_code)
            return []
        return result.json()

    def get_meal(self, meal_id: int, meal_title: str) -> types.Recipe:
        """Returns meal details as a schema.org Recipe.

        Uses the JSON API (same source as get_meals_detailed) because the
        update_meal_ingredients page migrated to Next.js and no longer serves
        usable HTML.

        meal_title is accepted for backward compatibility but ignored; the
        authoritative name comes from the API response.

        See https://schema.org/Recipe for schema details.
        """
        meals = self.get_meals_detailed()
        meal = next(
            (m for m in meals if int(m["meal_id"]) == int(meal_id)),
            None,
        )
        if meal is None:
            raise ValueError(f"Meal {meal_id!r} not found")

        foods = meal.get("foods", [])
        recipe_dict: dict[str, Any] = {
            "@context": "https://schema.org",
            "@type": "Recipe",
            "author": self.effective_username,
            "name": meal["description"],
            "recipeYield": 1,
            "recipeIngredient": [f["description"] for f in foods],
            "recipeInstructions": "",
            "tags": ["MyFitnessPal"],
        }

        if foods:
            recipe_dict["nutrition"] = {
                "@type": "NutritionInformation",
                "calories": sum(f.get("calories", 0) for f in foods),
                "carbohydrateContent": sum(f.get("carbs", 0) for f in foods),
                "fatContent": sum(f.get("fat", 0) for f in foods),
                "proteinContent": sum(f.get("protein", 0) for f in foods),
                "sodiumContent": sum(f.get("sodium", 0) for f in foods),
                "sugarContent": sum(f.get("sugar", 0) for f in foods),
            }

        return cast(types.Recipe, recipe_dict)

    # ============================================================================
    # Diary Write Operations
    # ============================================================================

    def delete_diary_entry(
        self, meal: str, entry_index: int, date: datetime.date
    ) -> tuple[str, str]:
        """Delete a food entry from the diary by meal and index.

        Returns:
            (entry_id, entry_name)
        """
        import re

        date_str = date.strftime("%Y-%m-%d")
        diary_url = parse.urljoin(
            self.BASE_URL_SECURE,
            f"food/diary/{self.effective_username}?date={date_str}",
        )
        document = self._get_document_for_url(diary_url)

        # Use the library's _get_meals to identify the entry by position
        meals = self._get_meals(document)
        target_meal = None
        for m in meals:
            if m.name.lower() == meal.lower():
                target_meal = m
                break

        if target_meal is None:
            available = ", ".join(m.name.title() for m in meals)
            raise ValueError(f"Meal '{meal}' not found. Available: {available}")

        if entry_index >= len(target_meal.entries):
            raise ValueError(
                f"Entry index {entry_index} out of range for {meal}. "
                f"Found {len(target_meal.entries)} entries."
            )

        entry_name = target_meal.entries[entry_index].name

        # Walk the HTML to find the delete link for this entry by position
        headers = document.xpath("//tr[@class='meal_header']")
        for header in headers:
            tds = header.findall("td")
            if not tds:
                continue
            meal_name = (tds[0].text or "").strip().lower()
            if meal_name != meal.lower():
                continue

            idx = 0
            current = header
            while True:
                current = current.getnext()
                if current is None:
                    break
                if current.attrib.get("class") is not None:
                    break
                delete_link = current.xpath('.//td[@class="delete"]//a/@href')
                if not delete_link:
                    continue
                if idx == entry_index:
                    match = re.search(r"/food/remove/(\d+)", delete_link[0])
                    if not match:
                        raise MyfitnesspalRequestFailed(
                            "Could not extract entry ID from delete link"
                        )
                    entry_id = match.group(1)

                    # Extract CSRF token and DELETE the entry
                    csrf_tokens = document.xpath(
                        '//meta[@name="csrf-token"]/@content'
                    )
                    if not csrf_tokens:
                        raise MyfitnesspalRequestFailed(
                            "Could not find CSRF token on diary page"
                        )
                    csrf_token = csrf_tokens[0]

                    remove_url = parse.urljoin(
                        self.BASE_URL_SECURE, f"food/remove/{entry_id}"
                    )
                    result = self.session.delete(
                        remove_url,
                        headers={
                            "Referer": diary_url,
                            "X-CSRF-Token": csrf_token,
                            "X-Requested-With": "XMLHttpRequest",
                        },
                    )
                    if not result.ok:
                        raise MyfitnesspalRequestFailed(
                            f"Failed to delete entry: status code {result.status_code}"
                        )

                    return entry_id, entry_name
                idx += 1

            raise ValueError(
                f"Entry index {entry_index} out of range for {meal} in HTML. "
                f"Found {idx} entries."
            )

        raise ValueError(f"Meal '{meal}' not found in HTML.")

    def add_food_to_diary(
        self,
        food_id: int,
        meal: str,
        date: datetime.date,
        quantity: float = 1.0,
        weight_id: str | None = None,
    ) -> None:
        """Add a food item to the diary.

        Args:
            food_id: MFP food item ID (from get_food_search_results or get_food_item_details)
            meal: Meal name (Breakfast, Lunch, Dinner, Snacks)
            date: Date to add the entry
            quantity: Number of servings (default 1.0)
            weight_id: Serving size ID. If None, uses the first serving from food details.
        """
        meal_index = self.MEAL_MAP.get(meal.lower())
        if meal_index is None:
            raise ValueError(
                f"Invalid meal '{meal}'. Must be one of: {', '.join(self.MEAL_MAP)}"
            )

        # /food/add only accepts old-format (~10-digit) IDs. New-format (15-digit)
        # IDs come from the mobile v2 API and are silently ignored by this endpoint.
        # Use mfp_search_food to get old-format mfp_id and weight_ids.
        NEW_ID_THRESHOLD = 10 ** 11
        if food_id > NEW_ID_THRESHOLD:
            raise ValueError(
                f"food_id {food_id} is a new-format ID (>10^11) that /food/add does not support. "
                "Use mfp_search_food to get an old-format mfp_id and pass its weight_ids[0] as weight_id."
            )
        if weight_id and int(weight_id) > NEW_ID_THRESHOLD:
            raise ValueError(
                f"weight_id {weight_id} is a new-format ID (>10^11) that /food/add does not support. "
                "Use the weight_ids list from mfp_search_food results instead."
            )

        search_url = parse.urljoin(self.BASE_URL_SECURE, self.SEARCH_PATH)
        doc = self._get_document_for_url(search_url)
        csrf_tokens = doc.xpath('//meta[@name="csrf-token"]/@content')
        if not csrf_tokens:
            raise MyfitnesspalRequestFailed(
                "Could not find CSRF token on food search page"
            )
        csrf_token = csrf_tokens[0]

        add_url = parse.urljoin(self.BASE_URL_SECURE, "food/add")
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "mfp-client-id": "mfp-main-js",
            "mfp-user-id": str(self.user_id),
            "Origin": self.BASE_URL_SECURE.rstrip("/"),
            "Referer": search_url,
            "X-CSRF-Token": csrf_token,
            "X-Requested-With": "XMLHttpRequest",
        }
        post_data = {
            "food_entry[food_id]": str(food_id),
            "food_entry[date]": date.strftime("%Y-%m-%d"),
            "food_entry[quantity]": str(quantity),
            "food_entry[weight_id]": str(weight_id),
            "food_entry[meal_id]": meal_index,
            "ajax": "true",
        }
        result = self.session.post(add_url, data=post_data, headers=headers)
        if not result.ok:
            raise MyfitnesspalRequestFailed(
                f"Failed to add food to diary: status code {result.status_code}, "
                f"response: {result.text[:200]}"
            )
        if result.status_code != 204:
            raise MyfitnesspalRequestFailed(
                f"Unexpected response adding food to diary: status code {result.status_code}, "
                f"response: {result.text[:500]}"
            )

    def quick_add_to_diary(
        self,
        meal: str,
        date: datetime.date,
        calories: float,
        protein: float = 0,
        carbohydrates: float = 0,
        fat: float = 0,
    ) -> None:
        """Quick-add calories and macros to a meal without specifying a food item.

        Args:
            meal: Meal name (Breakfast, Lunch, Dinner, Snacks)
            date: Date to add the entry
            calories: Number of calories
            protein: Grams of protein (default 0)
            carbohydrates: Grams of carbohydrates (default 0)
            fat: Grams of fat (default 0)
        """
        # Validate meal name; meal_name is passed as a string to the API
        meal_lower = meal.lower()
        if meal_lower not in self.MEAL_MAP:
            raise ValueError(
                f"Invalid meal '{meal}'. Must be one of: {', '.join(self.MEAL_MAP)}"
            )
        # API expects title-case meal name (e.g. "Lunch")
        meal_name = meal.capitalize() if meal_lower != "snacks" else "Snacks"

        # Get NextAuth CSRF token from /api/auth/csrf
        csrf_url = parse.urljoin(self.BASE_URL_SECURE, self.CSRF_PATH)
        csrf_resp = self.session.get(csrf_url)
        if not csrf_resp.ok:
            raise MyfitnesspalRequestFailed(
                f"Could not fetch CSRF token: status {csrf_resp.status_code}"
            )
        csrf_token = csrf_resp.json().get("csrfToken", "")

        diary_url = parse.urljoin(self.BASE_URL_SECURE, "api/services/diary")
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": self.BASE_URL_SECURE.rstrip("/"),
            "Referer": f"{self.BASE_URL_SECURE.rstrip('/')}/food/quick-add?meal={self.MEAL_MAP[meal_lower]}",
            "x-csrf-token": csrf_token,
        }
        payload = {
            "items": [
                {
                    "meal_name": meal_name,
                    "date": date.strftime("%Y-%m-%d"),
                    "nutritional_contents": {
                        "fat": fat,
                        "carbohydrates": carbohydrates,
                        "protein": protein,
                        "energy": {"value": calories, "unit": "calories"},
                    },
                    "type": "quick_add",
                }
            ]
        }
        result = self.session.post(diary_url, json=payload, headers=headers)
        if not result.ok:
            raise MyfitnesspalRequestFailed(
                f"Failed to quick-add to diary: status code {result.status_code}, "
                f"response: {result.text[:500]}"
            )

    def load_meals(self, meal_index: int) -> list[dict[str, str]]:
        """Fetch all saved meals from /food/load_meals, paginating through results.

        Visits /user/{username}/diary/add first to establish pagination state,
        then paginates through load_meals to return saved meal groups.

        Returns a list of dicts with keys: food_id, weight_id, name, index.
        """
        import lxml.html

        date_str = datetime.datetime.now().date().strftime("%Y-%m-%d")
        add_diary_url = parse.urljoin(
            self.BASE_URL_SECURE,
            f"food/add_to_diary?meal={meal_index}&date={date_str}",
        )
        load_meals_url = parse.urljoin(self.BASE_URL_SECURE, "food/load_meals")

        resp = self.session.get(add_diary_url)
        add_diary_doc = lxml.html.document_fromstring(resp.text)
        csrf_tokens = add_diary_doc.xpath('//meta[@name="csrf-token"]/@content')
        csrf_token = csrf_tokens[0] if csrf_tokens else ""

        all_meals: list[dict[str, str]] = []
        seen_food_ids: set[str] = set()
        base_index = 0
        page = 1

        while True:
            resp = self.session.post(
                load_meals_url,
                data={
                    "meal": str(meal_index),
                    "base_index": str(base_index),
                    "page": str(page),
                },
                headers={
                    "Accept": "text/html, */*; q=0.01",
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "X-Requested-With": "XMLHttpRequest",
                    "X-CSRF-Token": csrf_token,
                    "Origin": self.BASE_URL_SECURE,
                    "Referer": add_diary_url,
                },
            )

            if not resp.ok:
                logger.warning(
                    "load_meals base_index=%s returned HTTP %s",
                    base_index,
                    resp.status_code,
                )
                break

            doc = lxml.html.document_fromstring(resp.text)
            page_meals: list[dict[str, str]] = []

            for row in doc.xpath('//tr[contains(@class,"favorite")]'):
                cb = row.xpath('.//input[@type="checkbox"]')
                if not cb:
                    continue
                idx = (
                    cb[0]
                    .get("name", "")
                    .replace("favorites[", "")
                    .replace("][checked]", "")
                )
                hidden = row.xpath(
                    f'.//input[@type="hidden"][contains(@name,"favorites[{idx}][food_id]")]'
                )
                if not hidden:
                    continue
                food_id = hidden[0].get("value", "")
                name_td = row.xpath(".//td[2]")
                name = name_td[0].text_content().strip() if name_td else ""

                weight_id = ""
                weight_sel = row.xpath(f'.//select[@name="favorites[{idx}][weight_id]"]')
                if weight_sel:
                    selected = weight_sel[0].xpath(".//option[@selected]/@value")
                    if selected:
                        weight_id = selected[0]
                    else:
                        first = weight_sel[0].xpath(".//option[1]/@value")
                        if first:
                            weight_id = first[0]

                if food_id and name:
                    page_meals.append(
                        {
                            "food_id": food_id,
                            "weight_id": weight_id,
                            "name": name,
                            "index": idx,
                        }
                    )

            new_meals = [m for m in page_meals if m["food_id"] not in seen_food_ids]
            if not new_meals:
                break

            for m in new_meals:
                seen_food_ids.add(m["food_id"])
                all_meals.append(m)

            if len(page_meals) < 25:
                break

            base_index += len(page_meals)
            page += 1

        return all_meals

    def log_saved_meal(
        self, meal_name: str, diary_meal: str, date: datetime.date
    ) -> dict[str, str]:
        """Log a saved meal to the food diary via load_meals + add_favorites.

        Returns:
            Dict with keys: food_id, weight_id, name, index.
        """
        import lxml.html

        meal_index = self.MEAL_MAP.get(diary_meal.lower(), 2)
        date_str = date.strftime("%Y-%m-%d")

        add_diary_url = parse.urljoin(
            self.BASE_URL_SECURE,
            f"food/add_to_diary?meal={meal_index}&date={date_str}",
        )

        # Get authenticity_token for the add_favorites POST (also primes load_meals state)
        resp = self.session.get(add_diary_url)
        doc = lxml.html.document_fromstring(resp.text)
        form = doc.xpath("//form[contains(@action, 'add_favorites')][1]")
        if not form:
            raise MyfitnesspalRequestFailed(
                "Could not find add_favorites form on add_to_diary page"
            )
        at = form[0].xpath(".//input[@name='authenticity_token']/@value")
        if not at:
            raise MyfitnesspalRequestFailed(
                "Could not find authenticity_token on add_to_diary page"
            )
        authenticity_token = at[0]

        # Use load_meals to paginate and find the target meal by name
        all_meals = self.load_meals(meal_index)
        meal_entry = next(
            (m for m in all_meals
             if meal_name.lower() in m["name"].lower() or m["name"].lower() in meal_name.lower()),
            None,
        )

        if meal_entry is None:
            available = ", ".join(m["name"] for m in all_meals[:10])
            raise ValueError(
                f"Meal '{meal_name}' not found. "
                f"Available meals: {available}"
            )

        add_url = parse.urljoin(self.BASE_URL_SECURE, "food/add_favorites")
        post_data = {
            "authenticity_token": authenticity_token,
            "meal": str(meal_index),
            "date": date_str,
            f"favorites[{meal_entry['index']}][food_id]": meal_entry["food_id"],
            f"favorites[{meal_entry['index']}][checked]": "1",
            f"favorites[{meal_entry['index']}][quantity]": "1",
            f"favorites[{meal_entry['index']}][weight_id]": meal_entry["weight_id"],
            "add": "Add Checked",
        }

        result = self.session.post(
            add_url,
            data=post_data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": add_diary_url,
                "Origin": self.BASE_URL_SECURE,
            },
        )

        if not result.ok:
            raise MyfitnesspalRequestFailed(
                f"add_favorites returned HTTP {result.status_code}"
            )

        return meal_entry
