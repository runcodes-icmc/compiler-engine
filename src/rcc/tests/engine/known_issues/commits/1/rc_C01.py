while True:
    while True:
        peso = float(input("Peso (kg): "))
        if peso == 0 or 40 <= peso <= 200:
            break
    if peso == 0:
        break
    while True:
        copos = int(input("Número de copos de 150ml de cerveja consumidos: "))
        if 1 <= copos <= 20:
            break
    while True:
        jejum = input("Em jejum (S/N)? ").upper()
        if jejum == "S" or jejum == "N":
            break

    if jejum == "N":
        coeficiente = 1.1
    else:
        while True:
            sexo = input("Sexo (M/F)? ").upper()
            if sexo == "M" or sexo == "F":
                break
        if sexo == "M":
            coeficiente = 0.7
        else:
            coeficiente = 0.6

    taxa = (copos * 4.8) / (peso * coeficiente)
    print("\nTaxa de alcoolemia: %.1f g/l" % (taxa))

    if taxa > 0.2:
        print("Você bebeu demais!\n")
    else:
        print("Pode dirigir. Você é uma pessoa responsável.\n")
