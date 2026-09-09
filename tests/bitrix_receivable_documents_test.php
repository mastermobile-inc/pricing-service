<?php

namespace Bitrix\Main {
    final class Event
    {
        private array $parameters;

        public function __construct(array $parameters)
        {
            $this->parameters = $parameters;
        }

        public function getParameters(): array
        {
            return $this->parameters;
        }
    }

    final class EventResult
    {
        public const SUCCESS = 1;
        public string $html;

        public function __construct(int $type, string $html)
        {
            $this->html = $html;
        }
    }

    final class EventManager
    {
        public static $handler;

        public static function getInstance(): self
        {
            return new self();
        }

        public function addEventHandler(string $module, string $event, callable $handler): void
        {
            if ($module !== 'main' || $event !== 'onGetPublicView') {
                throw new \RuntimeException('Wrong event registration');
            }
            self::$handler = $handler;
        }
    }
}

namespace {
    require $argv[1] ?? __DIR__ . '/../infra/bitrix/receivable_documents.php';

    $checks = 0;
    function check(bool $condition, string $name): void
    {
        global $checks;
        if (!$condition) {
            throw new RuntimeException($name);
        }
        $checks++;
    }

    function field(string $value): array
    {
        return [
            'ENTITY_ID' => 'CRM_35',
            'FIELD_NAME' => 'UF_CRM_35_CHAINDOCUMENTS',
            'VALUE' => $value,
        ];
    }

    $open = '1. Открытый долг TEST-1 от 10.06.2026 09:00 на 12 000 руб. '
        . '(исходно 15 000 руб.; закрытия -3 000 руб.; возвраты 0 руб.; '
        . 'правило: onec_canonical_continuous_balance_origin)';
    $untouched = '2. Открытый долг TEST-2 от 11.06.2026 10:05 на 500.25 руб. '
        . '(исходно 500.25 руб.; закрытия 0 руб.; возвраты 0 руб.; правило: confirmed_open)';
    $html = MMReceivableDocumentsView::render(field($open . "\n" . $untouched));

    check($html !== null, 'Open documents render');
    check(str_contains($html, '<table'), 'Real HTML table');
    check(str_contains($html, 'data-editor-control-type="button"'), 'Native editor ignores interactions inside the table');
    check(!str_contains($html, 'stopPropagation') && !str_contains($html, 'preventDefault'), 'Native disclosure and selection remain unmodified');
    check(substr_count($html, '<tbody>') === 2, 'Main and adjustment tables');
    check(str_contains($html, '<th scope="col">Номер</th>'), 'Number column');
    check(str_contains($html, '<th scope="col">Дата</th>'), 'Date column');
    check(str_contains($html, 'Остаток, ₽'), 'Remaining debt label');
    check(str_contains($html, '<td>10.06.2026</td>'), 'Date without time');
    check(!str_contains($html, '09:00'), 'No redundant time');
    check(str_contains($html, "12\u{202F}000,00"), 'Russian money');
    check(str_contains($html, "12\u{202F}500,25"), 'Exact document total');
    check(!str_contains($html, 'Открытый долг'), 'No repeated debt label');
    check(!str_contains($html, 'исходно'), 'No prose details');
    check(!str_contains($html, 'onec_'), 'No technical rule');
    check(!str_contains($html, 'правило:'), 'No rule prefix');
    check(str_contains($html, '<details><summary>Погашения и возвраты (1)</summary>'), 'Collapsed relevant adjustments only');
    check(!str_contains($html, 'Возвраты, ₽'), 'All-zero returns column hidden');
    check(str_contains($html, "3\u{202F}000,00"), 'Closing shown as reduction');
    check(str_contains($html, 'не обязательно отдельная оплата'), 'No false payment claim');

    $plain = MMReceivableDocumentsView::render(field($untouched));
    check(!str_contains($plain, '<details>'), 'No empty adjustments');
    check(substr_count($plain, '<th ') === 3, 'Only three primary columns');

    $returned = str_replace('возвраты 0 руб.', 'возвраты 200 руб.', $open);
    check(str_contains(MMReceivableDocumentsView::render(field($returned)), 'Возвраты, ₽'), 'Nonzero return retained');

    $positiveClosing = str_replace('закрытия -3 000 руб.', 'закрытия 3 000 руб.', $open);
    check(str_contains(MMReceivableDocumentsView::render(field($positiveClosing)), "3\u{202F}000,00"), 'Both source closing conventions');

    $legacy = "1. Реализация SALE-1 от 10.06.2026 09:00 на 150 руб.\n"
        . "2. Оплата PAY-1 от 11.06.2026 10:00 на -25 руб.";
    $legacyHtml = MMReceivableDocumentsView::render(field($legacy));
    check(str_contains($legacyHtml, '<th scope="col">Вид</th>'), 'Mixed event types remain distinguishable');
    check(str_contains($legacyHtml, '−25,00'), 'Signed legacy payment');
    check(str_contains($legacyHtml, 'Сумма, ₽'), 'Legacy amount not mislabeled as open debt');

    $zero = str_replace('12 000 руб.', '0 руб.', $open);
    check(str_contains(MMReceivableDocumentsView::render(field($zero)), '<td>0,00</td>'), 'Zero is not replaced by gross amount');
    $cents = "1. Документ A на 0.01 руб.\n2. Документ B на 0.02 руб.";
    check(str_contains(MMReceivableDocumentsView::render(field($cents)), '<td>0,03</td>'), 'Integer kopek total');
    check(str_contains(MMReceivableDocumentsView::render(field($cents)), '<td>—</td>'), 'Missing date explicit');
    $large = '1. Документ A на 999999999999.99 руб.';
    check(str_contains(MMReceivableDocumentsView::render(field($large)), "999\u{202F}999\u{202F}999\u{202F}999,99"), 'Large amounts exact');
    check(MMReceivableDocumentsView::render(field('1. Документ A на 9999999999999.99 руб.')) === null, 'Oversized value safely rejected');

    $attack = str_replace('TEST-1', '<img src=x onerror=alert(1)> & "quoted"', $open);
    $escaped = MMReceivableDocumentsView::render(field($attack));
    check(!str_contains($escaped, '<img'), 'No injected HTML');
    check(str_contains($escaped, '&lt;img'), 'Original number safely escaped');
    check(str_contains($escaped, '&amp; &quot;quoted&quot;'), 'Quotes and ampersands escaped');
    check(MMReceivableDocumentsView::render(field($open . "\nUNKNOWN DOCUMENT")) === null, 'Unknown row does not disappear');
    check(MMReceivableDocumentsView::render(field(str_replace('исходно', 'новое поле', $open))) === null, 'Unknown details preserve original field');
    check(MMReceivableDocumentsView::render(field(str_replace('10.06.2026', '31.02.2026', $open))) === null, 'Invalid date preserves original');
    check(MMReceivableDocumentsView::render(field('')) === null, 'Empty native rendering retained');
    check(MMReceivableDocumentsView::render(field("\n \n")) === null, 'Whitespace native rendering retained');
    check(MMReceivableDocumentsView::render(field($open . "\r\n\r\n" . $untouched)) !== null, 'CRLF and blank lines');

    $otherField = field($open);
    $otherField['FIELD_NAME'] = 'UF_CRM_OTHER';
    check(MMReceivableDocumentsView::render($otherField) === null, 'Other fields untouched');
    $otherEntity = field($open);
    $otherEntity['ENTITY_ID'] = 'CRM_99';
    check(MMReceivableDocumentsView::render($otherEntity) === null, 'Other processes untouched');
    $invalidValue = field($open);
    $invalidValue['VALUE'] = ['unexpected array'];
    check(MMReceivableDocumentsView::render($invalidValue) === null, 'Unexpected field shape untouched');

    $handler = \Bitrix\Main\EventManager::$handler;
    check(is_callable($handler), 'Native event registered');
    $eventResult = $handler(new \Bitrix\Main\Event([field($open), []]));
    check($eventResult instanceof \Bitrix\Main\EventResult, 'Native event supplies custom view');
    check(str_contains($eventResult->html, '<table'), 'Native event returns table');
    check($handler(new \Bitrix\Main\Event([$otherField, []])) === null, 'Other native event ignored');

    echo "PASS: $checks assertions\n";
}
