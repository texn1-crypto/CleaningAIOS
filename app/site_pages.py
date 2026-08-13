from __future__ import annotations

import re
from html import escape
from math import ceil

from .config import settings


SERVICE_IMAGE_SIZES = {
    "/static/cleaning-hero.png": (1536, 1024),
    "/static/social/2026-08-13-business-center.png": (1254, 1254),
    "/static/social/2026-08-13-checklist-quality.png": (1254, 1254),
    "/static/services/business-center-lobby-v1.jpg": (1600, 1066),
    "/static/services/residential-lobby-v1.jpg": (1600, 876),
    "/static/services/warehouse-machine-v1.jpg": (1600, 777),
    "/static/services/facade-territory-v1.jpg": (1600, 1066),
}


SERVICE_DETAILS = {
    "offices": {
        "title": "Уборка офисов",
        "lead": "Поддерживаем рабочее пространство в порядке до, во время или после рабочего дня.",
        "image": "/static/services/business-center-lobby-v1.jpg",
        "zones": ("рабочие места и переговорные", "входные группы и коридоры", "кухни и санитарные зоны", "локальные заявки в течение смены"),
    },
    "business-centers": {
        "title": "Уборка бизнес-центров",
        "lead": "Сервис для объектов с постоянным потоком арендаторов, посетителей и подрядчиков.",
        "image": "/static/services/business-center-lobby-v1.jpg",
        "zones": ("лобби, лифты и общие зоны", "дневная дежурная служба", "контроль расходных материалов", "чек-листы и история замечаний"),
    },
    "residential": {
        "title": "Уборка ЖК и МКД",
        "lead": "Регламентная уборка подъездов, паркинга и территории с понятной отчётностью для УК и ТСЖ.",
        "image": "/static/services/residential-lobby-v1.jpg",
        "zones": ("подъезды и лестничные площадки", "лифтовые холлы", "паркинги и технические зоны", "придомовая территория"),
    },
    "retail": {
        "title": "Уборка магазинов и торговых центров",
        "lead": "Поддерживаем чистоту торгового пространства без помех для покупателей и персонала.",
        "image": "/static/social/2026-08-13-business-center.png",
        "zones": ("торговый зал и витрины", "кассовые и входные зоны", "склады и служебные помещения", "оперативная уборка загрязнений"),
    },
    "industrial": {
        "title": "Производственные помещения",
        "lead": "Состав работ и техника подбираются после обследования технологических и безопасностных требований объекта.",
        "image": "/static/services/warehouse-machine-v1.jpg",
        "zones": ("производственные участки", "склады и проходы", "административно-бытовые зоны", "контроль допусков и инструктажей"),
    },
    "warehouses": {
        "title": "Уборка складов",
        "lead": "Плановая и разовая уборка складских комплексов с учётом движения техники и товарных потоков.",
        "image": "/static/services/warehouse-machine-v1.jpg",
        "zones": ("погрузочные зоны", "проезды и стеллажные проходы", "служебные помещения", "машинная уборка пола"),
    },
    "restaurants": {
        "title": "Уборка ресторанов и кафе",
        "lead": "Чистота гостевых и служебных зон по согласованному графику и чек-листу.",
        "image": "/static/social/2026-08-13-business-center.png",
        "zones": ("гостевой зал", "входная группа", "санитарные комнаты", "служебные зоны в согласованном объёме"),
    },
    "medical": {
        "title": "Уборка медицинских объектов",
        "lead": "Проектирование процесса только после проверки обязательных санитарных требований и допусков.",
        "image": "/static/social/2026-08-13-checklist-quality.png",
        "zones": ("зоны ожидания и коридоры", "кабинеты по утверждённому регламенту", "санитарные зоны", "раздельный инвентарь и контроль"),
    },
    "general": {
        "title": "Генеральная уборка",
        "lead": "Глубокая разовая уборка помещения с заранее зафиксированным перечнем работ.",
        "image": "/static/cleaning-hero.png",
        "zones": ("труднодоступные поверхности", "мебель и оборудование снаружи", "стеклянные поверхности", "финальная проверка по чек-листу"),
    },
    "after-construction": {
        "title": "Уборка после ремонта",
        "lead": "Удаляем строительную пыль и готовим объект к работе или передаче.",
        "image": "/static/social/2026-08-13-business-center.png",
        "zones": ("обеспыливание поверхностей", "очистка пола и остекления", "локальное удаление следов материалов", "подготовка к приёмке"),
    },
    "facades": {
        "title": "Мойка фасадов и витрин",
        "lead": "Метод, техника и условия безопасности определяются после осмотра высоты, материала и доступа.",
        "image": "/static/services/facade-territory-v1.jpg",
        "zones": ("витрины и входное остекление", "фасадные панели", "вывески без вмешательства в электрику", "сезонное обслуживание"),
    },
    "territory": {
        "title": "Уборка территории и снега",
        "lead": "Сезонный график для дворов, пешеходных зон, парковок и входных групп.",
        "image": "/static/services/facade-territory-v1.jpg",
        "zones": ("пешеходные дорожки", "урны и площадки", "снег и противогололёдная обработка", "оперативные выходы по погоде"),
    },
}


PRICE_ROWS = (
    ("Уборка офисов", 55, 45, 65), ("Уборка бизнес-центров", 60, 40, 75),
    ("Уборка административных зданий", 55, 40, 70), ("Уборка офисного здания", 60, 45, 65),
    ("Уборка коттеджей", 65, 55, 70), ("Уборка квартир", 60, 50, 75),
    ("Уборка офисных помещений", 55, 40, 70), ("Уборка производственных помещений", 65, 50, 75),
    ("Уборка складских помещений", 60, 45, 70), ("Уборка коворкинга", 55, 40, 65),
    ("Уборка парка", 60, 45, 70), ("Уборка территории", 55, 40, 65),
    ("Поддерживающая уборка", 60, 45, 70), ("Генеральная уборка", 65, 55, 75),
    ("Уборка магазинов", 55, 45, 65), ("Уборка торговых центров", 60, 40, 75),
    ("Уборка супермаркетов", 55, 45, 70), ("Уборка автосалонов", 60, 40, 65),
    ("Уборка баров", 65, 50, 70), ("Уборка ресторанов", 60, 45, 75),
    ("Уборка кафе", 55, 40, 65), ("Уборка салонов красоты", 60, 45, 70),
    ("Уборка барбершопа", 55, 40, 65), ("Уборка клиник", 65, 55, 75),
    ("Уборка больниц", 70, 60, 75), ("Уборка стоматологии", 65, 55, 70),
    ("Уборка гостиниц", 60, 45, 65), ("Уборка отелей", 65, 55, 70),
    ("Уборка учебных заведений", 60, 40, 75), ("Уборка школы", 60, 40, 75),
    ("Уборка фитнес-клубов", 60, 40, 75), ("Чистка бассейнов", 55, 45, 65),
    ("Уборка АЗС", 65, 55, 70), ("Уборка в лабораториях", 70, 60, 75),
    ("Клининг шатров", 60, 40, 65), ("Уборка аэропортов", 65, 45, 70),
    ("Уборка вокзалов", 60, 40, 75), ("Уборка государственных учреждений", 55, 45, 65),
    ("Уборка театров", 65, 55, 70), ("Уборка стадионов", 70, 60, 75),
    ("Уборка спортивных объектов", 60, 45, 65), ("Уборка заводов", 65, 45, 70),
    ("Строительный клининг", 60, 45, 75),
)


def _price(reference: int) -> int:
    """Return a whole price strictly lower, with a discount no greater than 5%."""
    return min(reference - 1, ceil(reference * 0.95))


def _social_links() -> str:
    rows = (
        ("Telegram", settings.social_telegram_url),
        ("VK", settings.social_vk_url),
        ("Одноклассники", settings.social_odnoklassniki_url),
        ("Instagram", settings.social_instagram_url),
    )
    links = []
    for label, url in rows:
        if url.startswith("https://"):
            links.append(f'<a href="{escape(url, quote=True)}" rel="noopener noreferrer" target="_blank">{label}</a>')
    return "".join(links)


def _layout(*, title: str, description: str, body: str, active: str = "") -> str:
    company = escape(settings.company_name)
    nav = (
        ("services", "/services", "Услуги"),
        ("prices", "/prices", "Цены"),
        ("about", "/about", "О компании"),
        ("journal", "/journal", "Журнал"),
        ("contacts", "/contacts", "Контакты"),
    )
    links = "".join(f'<a class="{"active" if key == active else ""}" href="{href}">{label}</a>' for key, href, label in nav)
    social = _social_links()
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="{escape(description, quote=True)}"><meta name="theme-color" content="#f4f4f0">
<title>{escape(title)} · {company}</title><link rel="stylesheet" href="/static/site.css"><link rel="stylesheet" href="/static/service-imagery.css"><script defer src="/static/site.js"></script></head>
<body class="subpage"><header class="site-header" data-header><a class="brand" href="/"><span class="brand-dot"></span>{company}</a>
<button class="menu-toggle" type="button" aria-label="Открыть меню" aria-expanded="false" data-menu-toggle>Меню</button>
<nav class="nav" data-nav aria-label="Основная навигация">{links}<a class="nav-cta" href="/#request">Рассчитать стоимость</a></nav></header>
<main>{body}</main>
<footer><div><a class="brand footer-brand" href="/"><span class="brand-dot"></span>{company}</a><p>Клининг для объектов, где важны стабильность, безопасность и проверяемый результат.</p></div>
<div><span>Разделы</span><a href="/services">Услуги</a><a href="/prices">Цены</a><a href="/contacts">Контакты</a></div>
<div><span>Соцсети</span><div class="social-links" data-social-links>{social}</div><a href="/privacy">Конфиденциальность</a></div>
<div class="footer-bottom">© <span data-year></span> {company}</div></footer></body></html>"""


def services_html() -> str:
    cards = "".join(
        f"""<a class="catalog-card reveal" href="/services/{slug}"><img src="{row['image']}" alt="" loading="lazy" decoding="async" width="{SERVICE_IMAGE_SIZES[row['image']][0]}" height="{SERVICE_IMAGE_SIZES[row['image']][1]}"><span>{index:02d}</span><h2>{escape(row['title'])}</h2><p>{escape(row['lead'])}</p><b>Подробнее →</b></a>"""
        for index, (slug, row) in enumerate(SERVICE_DETAILS.items(), 1)
    )
    body = f"""<section class="page-hero"><p class="eyebrow"><span></span> Каталог</p><h1>Клининг под задачу объекта.</h1><p>Выберите тип объекта. Точный состав работ и стоимость фиксируются после аудита.</p></section>
<section class="section catalog-grid">{cards}</section><section class="page-cta"><h2>Не нашли точное название?</h2><p>Опишите объект — соберём регламент под ваши зоны, график и нагрузку.</p><a class="button primary" href="/#request">Обсудить объект</a></section>"""
    return _layout(title="Услуги клининга", description="Каталог услуг CleaningAIOS для бизнеса, ЖК и коммерческой недвижимости в Санкт-Петербурге и Ленинградской области.", body=body, active="services")


def service_html(slug: str) -> str | None:
    row = SERVICE_DETAILS.get(slug)
    if not row:
        return None
    image_width, image_height = SERVICE_IMAGE_SIZES[row["image"]]
    zones = "".join(f"<li>{escape(value)}</li>" for value in row["zones"])
    body = f"""<section class="service-detail-hero"><div><p class="eyebrow"><span></span> Услуга</p><h1>{escape(row['title'])}</h1><p>{escape(row['lead'])}</p><div class="hero-actions"><a class="button primary" href="/#request">Получить расчёт</a><a class="button ghost" href="/prices">Смотреть цены</a></div></div><img src="{row['image']}" alt="{escape(row['title'], quote=True)}" width="{image_width}" height="{image_height}" fetchpriority="high" decoding="async"></section>
<section class="section detail-grid"><div><p class="eyebrow"><span></span> Состав работ</p><h2>Что учитываем в плане</h2></div><ul class="feature-list">{zones}</ul></section>
<section class="section approach"><div class="approach-intro"><p class="eyebrow"><span></span> Процесс</p><h2>От обследования до стабильного сервиса.</h2></div><ol class="steps"><li><span>01</span><div><h3>Осмотр</h3><p>Фиксируем площади, трафик, покрытия и ограничения.</p></div></li><li><span>02</span><div><h3>Регламент</h3><p>Определяем операции, частоту, смены и точки контроля.</p></div></li><li><span>03</span><div><h3>Смета</h3><p>Согласовываем состав команды, материалы и границы ответственности.</p></div></li><li><span>04</span><div><h3>Контроль</h3><p>Ведём задачи и замечания в единой системе.</p></div></li></ol></section>"""
    return _layout(title=row["title"], description=row["lead"], body=body, active="services")


def prices_html() -> str:
    rows = "".join(
        f"<tr><th scope='row'>{escape(name)}</th><td>от {_price(general)} ₽</td><td>от {_price(regular)} ₽</td><td>от {_price(after)} ₽</td><td><a href='/#request'>Расчёт →</a></td></tr>"
        for name, general, regular, after in PRICE_ROWS
    )
    body = f"""<section class="page-hero"><p class="eyebrow"><span></span> Прайс-лист</p><h1>Цены на клининг для бизнеса.</h1><p>Ориентиры указаны за м² и округлены до целых рублей. Итоговая смета зависит от площади, графика, покрытий и состава работ.</p></section>
<section class="section price-section"><div class="table-wrap"><table class="price-table"><thead><tr><th>Объект</th><th>Генеральная</th><th>Поддерживающая</th><th>После ремонта</th><th></th></tr></thead><tbody>{rows}</tbody></table></div><p class="price-note">Цены являются предварительным ориентиром и не являются публичной офертой. Финальная стоимость фиксируется в коммерческом предложении после обследования объекта.</p></section>"""
    return _layout(title="Цены", description="Предварительные цены CleaningAIOS на генеральную, поддерживающую и послестроительную уборку.", body=body, active="prices")


def about_html() -> str:
    body = """<section class="page-hero"><p class="eyebrow"><span></span> CleaningAIOS</p><h1>Клининг как прозрачная операционная система.</h1><p>Мы строим сервис вокруг регламента, ответственных, контроля качества и понятной обратной связи.</p></section>
<section class="section value-grid"><article><span>01</span><h2>Факты вместо обещаний</h2><p>До старта фиксируем зоны, операции, частоту и критерии приёмки.</p></article><article><span>02</span><h2>Один контур управления</h2><p>Заявки, задачи, смены и замечания связаны с конкретным объектом.</p></article><article><span>03</span><h2>Решения с подтверждением</h2><p>Финансовые и договорные обязательства не принимаются без владельца.</p></article></section>
<section class="section proof"><img class="about-photo" src="/static/social/2026-08-13-checklist-quality.png" alt="Контроль качества клининга" width="1254" height="1254"><div><p class="eyebrow light"><span></span> Наш принцип</p><h2>Сначала система. Затем масштаб.</h2><p>Сервис должен оставаться управляемым на одном объекте и на сети площадок. Поэтому мы сохраняем историю действий и отделяем рекомендации AI от подтверждённых решений людей.</p></div></section>"""
    return _layout(title="О компании", description="Подход CleaningAIOS к профессиональному клинингу и контролю качества.", body=body, active="about")


def contacts_html() -> str:
    raw_phone = settings.company_phone.strip()
    phone = escape(raw_phone or "Телефон добавляется")
    phone_href = escape(re.sub(r"[^+\d]", "", raw_phone), quote=True)
    phone_value = f'<a href="tel:{phone_href}"><strong>{phone}</strong></a>' if phone_href else f"<strong>{phone}</strong>"
    email = escape(settings.company_email or "cleaningai@mail.ru")
    area = escape(settings.company_service_area or "Санкт-Петербург и Ленинградская область")
    social = _social_links() or "<span>Ссылки появятся после завершения регистрации официальных страниц.</span>"
    body = f"""<section class="page-hero"><p class="eyebrow"><span></span> Контакты</p><h1>Обсудим ваш объект.</h1><p>Для расчёта достаточно типа объекта, примерной площади, графика и желаемой даты старта.</p></section>
<section class="section contact-grid"><article><span>Телефон</span>{phone_value}</article><article><span>Email</span><a href="mailto:{email}"><strong>{email}</strong></a></article><article><span>Регион</span><strong>{area}</strong></article><article><span>Социальные сети</span><div class="social-links">{social}</div></article></section>
<section class="page-cta"><h2>Получить предварительный расчёт</h2><p>Заявка попадёт в CRM, а параметры объекта сохранятся в истории обращения.</p><a class="button primary" href="/#request">Заполнить форму</a></section>"""
    return _layout(title="Контакты", description="Контакты CleaningAIOS для расчёта клининга в Санкт-Петербурге и Ленинградской области.", body=body, active="contacts")


def journal_html() -> str:
    body = """<section class="page-hero"><p class="eyebrow"><span></span> Журнал</p><h1>Практика чистого объекта.</h1><p>Регламенты, контроль качества и решения для управляющих компаний и бизнеса.</p></section><section class="section"><div class="news-grid" data-news><article class="news-empty">Публикации появятся после визуального согласования владельцем.</article></div></section>"""
    return _layout(title="Журнал", description="Материалы CleaningAIOS о клининге, эксплуатации объектов и контроле качества.", body=body, active="journal")
