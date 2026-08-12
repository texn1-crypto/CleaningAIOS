from __future__ import annotations

from html import escape

from .config import settings


PUBLIC_SITE_HTML = r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="description" content="Профессиональный клининг для жилых комплексов, бизнес-центров и коммерческой недвижимости в Москве и Московской области.">
  <meta name="theme-color" content="#f5f5f2">
  <meta property="og:type" content="website">
  <meta property="og:title" content="CleaningAI — чистота как управляемый сервис">
  <meta property="og:description" content="Клининг для ЖК, БЦ и коммерческой недвижимости с прозрачным контролем качества.">
  <meta property="og:image" content="__OG_IMAGE_URL__">
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="CleaningAI — чистота как управляемый сервис">
  <meta name="twitter:description" content="Клининг для ЖК, БЦ и коммерческой недвижимости с прозрачным контролем качества.">
  <meta name="twitter:image" content="__OG_IMAGE_URL__">
  <title>CleaningAI — профессиональный клининг</title>
  <link rel="stylesheet" href="/static/site.css">
  <script defer src="/static/site.js"></script>
</head>
<body>
  <header class="site-header" data-header>
    <a class="brand" href="#top" aria-label="CleaningAI — на главную"><span class="brand-dot"></span><span data-company-name>CleaningAI</span></a>
    <button class="menu-toggle" type="button" aria-label="Открыть меню" aria-expanded="false" data-menu-toggle>Меню</button>
    <nav class="nav" aria-label="Основная навигация" data-nav>
      <a href="#services">Услуги</a><a href="#approach">Подход</a><a href="#news">Новости</a><a href="#contact">Контакты</a>
      <a class="nav-cta" href="#request">Рассчитать стоимость</a>
    </nav>
  </header>

  <main id="top">
    <section class="hero">
      <div class="hero-copy reveal">
        <p class="eyebrow"><span></span> Клининг как управляемый сервис</p>
        <h1>Порядок, который работает на ваш бизнес.</h1>
        <p class="hero-text">Берём на себя ежедневную и генеральную уборку жилых комплексов, бизнес-центров и коммерческих помещений. Планируем, контролируем и показываем результат.</p>
        <div class="hero-actions"><a class="button primary" href="#request">Обсудить объект</a><a class="button ghost" href="#approach">Как мы работаем</a></div>
        <div class="hero-trust" aria-label="Ключевые принципы"><span>Контроль качества</span><span>Прозрачная смета</span><span>Один ответственный</span></div>
      </div>
      <figure class="hero-visual reveal">
        <img src="/static/cleaning-hero.png" alt="Светлый современный интерьер после профессиональной уборки" width="1536" height="1024">
        <figcaption><strong>Чистота без микроменеджмента</strong><span>Команда, график и контроль в одном процессе</span></figcaption>
      </figure>
    </section>

    <section class="marquee" aria-label="Типы объектов"><span>ЖИЛЫЕ КОМПЛЕКСЫ</span><i></i><span>БИЗНЕС-ЦЕНТРЫ</span><i></i><span>КОММЕРЧЕСКИЕ ПОМЕЩЕНИЯ</span><i></i><span>ГЕНЕРАЛЬНАЯ УБОРКА</span></section>

    <section class="section" id="services">
      <div class="section-heading reveal"><p class="eyebrow"><span></span> Услуги</p><h2>Каждый объект — отдельная система.</h2><p>Подбираем состав работ, людей и контрольные точки под реальную нагрузку, а не под универсальный шаблон.</p></div>
      <div class="service-grid">
        <article class="service-card reveal"><div class="service-number">01</div><h3>ЖК и МКД</h3><p>Подъезды, лифты, входные группы, паркинги и придомовая территория.</p><ul><li>Регламент по зонам</li><li>Контроль заявок</li><li>Отчётность для УК и ТСЖ</li></ul></article>
        <article class="service-card reveal"><div class="service-number">02</div><h3>Бизнес-центры</h3><p>Ежедневная поддерживающая уборка без помех для сотрудников и посетителей.</p><ul><li>Дневные и ночные смены</li><li>Санитарные зоны</li><li>Резерв на замену</li></ul></article>
        <article class="service-card dark reveal"><div class="service-number">03</div><h3>Коммерческие объекты</h3><p>Офисы, торговые помещения и общие зоны с понятной экономикой объекта.</p><ul><li>Аудит до запуска</li><li>Материалы и инвентарь</li><li>Контроль SLA</li></ul></article>
      </div>
    </section>

    <section class="section approach" id="approach">
      <div class="approach-intro reveal"><p class="eyebrow"><span></span> Подход</p><h2>Сначала изучаем объект. Потом обещаем.</h2><p>Фиксируем зоны, частоту работ и критерии качества. Так смета остаётся предсказуемой, а команда понимает результат.</p></div>
      <ol class="steps">
        <li class="reveal"><span>01</span><div><h3>Аудит</h3><p>Осматриваем объект, трафик и сложные зоны.</p></div></li>
        <li class="reveal"><span>02</span><div><h3>План</h3><p>Считаем смены, материалы и график контроля.</p></div></li>
        <li class="reveal"><span>03</span><div><h3>Запуск</h3><p>Выводим команду и назначаем ответственного.</p></div></li>
        <li class="reveal"><span>04</span><div><h3>Контроль</h3><p>Ведём задачи, замечания и отчётность.</p></div></li>
      </ol>
    </section>

    <section class="section proof">
      <div class="proof-visual reveal"><img src="/static/cleaning-hero.png" alt="Деталь чистого современного пространства" loading="lazy" width="1536" height="1024"></div>
      <div class="proof-copy reveal"><p class="eyebrow light"><span></span> Управление качеством</p><h2>Не просто убираем. Держим процесс под контролем.</h2><div class="proof-list"><div><strong>Единое окно</strong><p>Все обращения и задачи по объекту собраны в одном контуре.</p></div><div><strong>Проверяемый результат</strong><p>Регламенты, ответственные и история действий не теряются в переписке.</p></div><div><strong>Без скрытых решений</strong><p>Изменения бюджета и обязательства проходят подтверждение владельца.</p></div></div></div>
    </section>

    <section class="section news-section" id="news">
      <div class="section-heading reveal"><p class="eyebrow"><span></span> Новости</p><h2>Что происходит в компании.</h2><p>Обновления проектов, полезные материалы и новые стандарты работы.</p></div>
      <div class="news-grid" data-news><article class="news-empty">Публикации появятся после подтверждения редактором.</article></div>
    </section>

    <section class="section request-section" id="request">
      <div class="request-copy reveal"><p class="eyebrow"><span></span> Новый объект</p><h2>Расскажите, что нужно поддерживать в порядке.</h2><p>Заявка попадёт в CRM. Срочные и крупные объекты система сразу отметит для приоритетного ответа специалиста.</p><div class="contact-card"><span>Работаем в регионе</span><strong data-service-area>Москва и Московская область</strong><a href="#" data-company-phone hidden></a><a href="#" data-company-email hidden></a></div></div>
      <form class="lead-form reveal" data-lead-form novalidate>
        <div class="field-row"><label><span>Ваше имя *</span><input name="name" autocomplete="name" required maxlength="120"></label><label><span>Компания</span><input name="company" autocomplete="organization" maxlength="255"></label></div>
        <div class="field-row"><label><span>Телефон</span><input name="phone" type="tel" autocomplete="tel" placeholder="+7 900 000-00-00" maxlength="32"></label><label><span>Email</span><input name="email" type="email" autocomplete="email" placeholder="name@company.ru"></label></div>
        <label><span>Тип объекта *</span><select name="service" required><option value="">Выберите</option><option value="mcd">ЖК / МКД</option><option value="business_center">Бизнес-центр</option><option value="commercial">Коммерческий объект</option><option value="general">Генеральная уборка</option><option value="other">Другое</option></select></label>
        <div class="field-row"><label><span>Площадь, м²</span><input name="object_area" type="number" min="0" step="1"></label><label><span>Когда нужен старт</span><select name="urgency"><option value="month">В течение месяца</option><option value="week">В течение недели</option><option value="today">Как можно скорее</option><option value="planning">Пока планируем</option></select></label></div>
        <label><span>Что важно учесть</span><textarea name="message" rows="4" maxlength="3000" placeholder="Опишите объект, график или текущую задачу"></textarea></label>
        <label class="honeypot" aria-hidden="true"><span>Website</span><input name="website" tabindex="-1" autocomplete="off"></label>
        <label class="consent"><input name="consent" type="checkbox" required><span>Я согласен(на) на обработку данных для ответа на заявку и принимаю <a href="/privacy" target="_blank">политику конфиденциальности</a>.</span></label>
        <button class="button primary form-submit" type="submit">Отправить заявку</button>
        <p class="form-status" role="status" aria-live="polite" data-form-status></p>
      </form>
    </section>
  </main>

  <footer id="contact"><div><a class="brand footer-brand" href="#top"><span class="brand-dot"></span><span data-company-name>CleaningAI</span></a><p>Профессиональный клининг для объектов, где важны стабильность и контроль.</p></div><div><span>Навигация</span><a href="#services">Услуги</a><a href="#approach">Подход</a><a href="#news">Новости</a></div><div><span>Документы</span><a href="/privacy">Конфиденциальность</a><a href="/mission-control">Mission Control</a></div><div class="footer-bottom">© <span data-year></span> <span data-company-name>CleaningAI</span></div></footer>
</body>
</html>"""


def privacy_html() -> str:
    operator = escape(settings.company_legal_name or settings.company_name)
    email = escape(settings.privacy_contact_email or settings.company_email or "не указан")
    address = escape(settings.company_address or "указывается владельцем до публикации сайта")
    inn = escape(settings.company_inn or "указывается владельцем до публикации сайта")
    return f"""<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>Политика конфиденциальности · {escape(settings.company_name)}</title><link rel=\"stylesheet\" href=\"/static/site.css\"></head><body class=\"policy-page\"><main class=\"policy\"><a class=\"back-link\" href=\"/\">← На сайт</a><p class=\"eyebrow\"><span></span> Редакция 1.0</p><h1>Политика обработки персональных данных</h1><p class=\"policy-lead\">Эта страница описывает обработку данных, переданных через форму заявки на сайте.</p><h2>1. Оператор</h2><p>{operator}, ИНН: {inn}, адрес: {address}. Контакт по вопросам персональных данных: {email}.</p><h2>2. Какие данные обрабатываются</h2><p>Имя, контактный телефон, email, название компании, параметры объекта, текст обращения и технические данные, необходимые для защиты формы от злоупотреблений.</p><h2>3. Цели и основание</h2><p>Данные используются для ответа на заявку, подготовки предложения, ведения истории обращения и защиты сервиса. Основание — согласие, которое пользователь даёт перед отправкой формы.</p><h2>4. Срок и защита</h2><p>Данные хранятся не дольше, чем это необходимо для работы с обращением и выполнения обязательных требований. Доступ ограничивается ролями, действия фиксируются в журнале, а внешним AI‑провайдерам передаётся только минимально необходимый и по возможности обезличенный контекст.</p><h2>5. Права пользователя</h2><p>Пользователь может запросить сведения об обработке, уточнение, блокирование или удаление данных, а также отозвать согласие, написав по указанному адресу.</p><h2>6. Передача третьим лицам</h2><p>Передача возможна только поставщикам инфраструктуры и связи в объёме, необходимом для работы сервиса, либо когда этого требует закон. Банковские пароли и платёжные ключи через сайт не собираются.</p><p class=\"policy-note\">Перед публичным запуском реквизиты оператора и финальную редакцию политики должен проверить владелец компании или профильный юрист.</p></main></body></html>"""
