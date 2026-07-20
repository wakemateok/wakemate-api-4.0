const FORMS = {
  zhBaseline: '16B9hHcTB5Gr74HzQR59lqxwHJhrgoTMh02ysKhT89bo',
  zhDaily: '1dBWuBDgE7vGsT_89cJrKz8E-w24i_ibqlhJ_1S6deMo',
  idBaseline: '1dUXvuFe_gKTIe0TypgV6LCON1bfQ6vJtZgXAb67ttRY',
  idDaily: '1y8DHJtdk_v8P5Qrbpc8LXpwJBX3q-5oWaMQrDZztvoA',
};

function updateWakeMateForms() {
  updateConfirmationMessage(
    FORMS.zhBaseline,
    '問卷已送出。請自行關閉問卷視窗，切換回 WakeMate App。'
  );
  updateConfirmationMessage(
    FORMS.zhDaily,
    '問卷已送出。請自行關閉問卷視窗，切換回 WakeMate App。'
  );
  updateConfirmationMessage(
    FORMS.idBaseline,
    'Kuesioner telah dikirim. Silakan tutup jendela browser dan kembali ke aplikasi WakeMate.'
  );
  updateConfirmationMessage(
    FORMS.idDaily,
    'Kuesioner telah dikirim. Silakan tutup jendela browser dan kembali ke aplikasi WakeMate.'
  );

  updateDailyCaffeineQuestion1(
    FORMS.zhDaily,
    ['七、咖啡因相關紀錄', '咖啡因相關紀錄', '咖啡因攝取紀錄'],
    ['飲用這杯含咖啡因', '您是幾點飲用', '含咖啡因飲品'],
    '若未飲用任何咖啡因飲料請略過填寫，直接點選完成。'
  );
  updateDailyCaffeineQuestion1(
    FORMS.idDaily,
    ['kafein', 'catatan kafein', 'konsumsi kafein'],
    ['kafein', 'berkafein', 'jam berapa'],
    'Jika tidak mengonsumsi minuman berkafein apa pun, lewati bagian ini dan langsung klik Selesai.'
  );
}

function updateConfirmationMessage(formId, message) {
  const form = FormApp.openById(formId);
  form.setConfirmationMessage(message);
  Logger.log(`${form.getTitle()} confirmation message updated`);
}

function updateDailyCaffeineQuestion1(formId, sectionKeywords, titleKeywords, noteText) {
  const form = FormApp.openById(formId);
  const items = form.getItems();
  const sectionIndex = findItemIndexByKeywords(items, sectionKeywords);
  let targetItem = null;

  if (sectionIndex >= 0) {
    for (let i = sectionIndex + 1; i < items.length; i += 1) {
      const item = items[i];
      if (isQuestionItem(item)) {
        targetItem = item;
        break;
      }
    }
  }

  if (targetItem === null) {
    targetItem = findQuestionByKeywords(items, titleKeywords);
  }

  if (targetItem === null) {
    Logger.log(`${form.getTitle()} did not find the daily caffeine question 1`);
    return;
  }

  appendNoteToItemTitle(targetItem, noteText);
  removeNoteFromItemHelpText(targetItem, noteText);
  Logger.log(`${form.getTitle()} updated daily caffeine question 1: ${targetItem.getTitle()}`);
}

function findItemIndexByKeywords(items, keywords) {
  for (let i = 0; i < items.length; i += 1) {
    if (matchesAnyKeyword(items[i].getTitle(), keywords)) return i;
  }
  return -1;
}

function findQuestionByKeywords(items, keywords) {
  for (const item of items) {
    if (isQuestionItem(item) && matchesAnyKeyword(item.getTitle(), keywords)) {
      return item;
    }
  }
  return null;
}

function appendNoteToItemTitle(item, noteText) {
  const wrappedNote = `（${noteText}）`;
  let title = item.getTitle();
  title = title.split(wrappedNote).join('');
  title = title.split(noteText).join('');
  title = title.replace(/\n\s*\n/g, '\n').trim();
  item.setTitle(`${title}\n${wrappedNote}`);
}

function removeNoteFromItemHelpText(item, noteText) {
  const typedItem = getTypedQuestionItem(item);
  if (typedItem === null || typeof typedItem.getHelpText !== 'function') return;

  const wrappedNote = `（${noteText}）`;
  let helpText = typedItem.getHelpText() || '';
  helpText = helpText.split(wrappedNote).join('');
  helpText = helpText.split(noteText).join('');
  helpText = helpText.replace(/\n\s*\n/g, '\n').trim();
  typedItem.setHelpText(helpText);
}

function isQuestionItem(item) {
  const questionTypes = [
    FormApp.ItemType.TEXT,
    FormApp.ItemType.PARAGRAPH_TEXT,
    FormApp.ItemType.MULTIPLE_CHOICE,
    FormApp.ItemType.CHECKBOX,
    FormApp.ItemType.LIST,
    FormApp.ItemType.SCALE,
    FormApp.ItemType.GRID,
    FormApp.ItemType.CHECKBOX_GRID,
    FormApp.ItemType.DATE,
    FormApp.ItemType.TIME,
    FormApp.ItemType.DATETIME,
    FormApp.ItemType.DURATION,
  ];

  return questionTypes.indexOf(item.getType()) !== -1;
}

function matchesAnyKeyword(title, keywords) {
  const normalizedTitle = String(title).toLowerCase();
  return keywords.some((keyword) =>
    normalizedTitle.indexOf(String(keyword).toLowerCase()) !== -1
  );
}

function setHelpTextIfSupported(item, helpText) {
  const typedItem = getTypedQuestionItem(item);
  if (typedItem === null) return false;
  typedItem.setHelpText(helpText);
  return true;
}

function getTypedQuestionItem(item) {
  const type = item.getType();

  if (type === FormApp.ItemType.TEXT) {
    return item.asTextItem();
  }
  if (type === FormApp.ItemType.PARAGRAPH_TEXT) {
    return item.asParagraphTextItem();
  }
  if (type === FormApp.ItemType.MULTIPLE_CHOICE) {
    return item.asMultipleChoiceItem();
  }
  if (type === FormApp.ItemType.CHECKBOX) {
    return item.asCheckboxItem();
  }
  if (type === FormApp.ItemType.LIST) {
    return item.asListItem();
  }
  if (type === FormApp.ItemType.DATE) {
    return item.asDateItem();
  }
  if (type === FormApp.ItemType.TIME) {
    return item.asTimeItem();
  }
  if (type === FormApp.ItemType.DATETIME) {
    return item.asDateTimeItem();
  }

  return null;
}
