#include <stdio.h>

int main(){

	float a,b,c;

	scanf("%f %f %f" , &a, &b, &c);

	if(b>c)
	{
		if (b>=a && c<=a)
			printf("CONTIDO");
		else
			printf("NAO CONTIDO");
	}
	else if (b<=a && c>=a)
		printf("CONTIDO");
	else
		printf("NAO CONTIDO");

    return 0;
}

